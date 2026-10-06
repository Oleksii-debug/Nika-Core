from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path
from typing import NoReturn

from nika_core.data.sqlite import SQLiteStore
from nika_core.learning_package import FrozenLearningPackage, LearningDataSplit
from nika_core.runtime.idempotency import IdempotencyLedger, IdempotencyStatus
from nika_core.training_peft_worker import (
    candidate_adapter_manifest,
    candidate_artifact_path,
)
from nika_core.training_physical_evaluation_driver import (
    _SCALE_PROGRESSION_OPERATION_TYPE,
    _find_pilot_task,
    _iter_task_idempotency_records,
    load_trusted_scale_progression_proof,
)
from nika_core.training_physical_pilot import PhysicalTrainingPilotReport

_TIER0_ID = "pilot"
_TIER1_ID = "scale-1"
_TIER1_STEPS = 3
_TIER1_JOB_ID = "physical-scale-proof-tier1-job"
_TIER1_CANDIDATE_REF = "models/nika-physical-scale-tier1-adapter"
_EXPERIMENT_ID = "physical-scale-progression-real-proof-v1"
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_JSON_BYTES = 1024 * 1024
_MAX_CANDIDATE_BYTES = 64 * 1024 * 1024
_WINDOWS_GENERIC_READ = 0x80000000
_WINDOWS_FILE_SHARE_READ = 0x00000001
_WINDOWS_OPEN_EXISTING = 3
_WINDOWS_FILE_ATTRIBUTE_NORMAL = 0x00000080
_WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_TIER1_REPLACED_CONFIG_FIELDS = frozenset(
    {
        "base_artifact_ref",
        "candidate_artifact_ref",
        "candidate_descriptor",
        "frozen_package_path",
        "frozen_package_sha256",
        "job_id",
        "output_root",
        "schema_version",
    }
)
_TIER1_ADDED_CONFIG_FIELDS = frozenset(
    {"initial_adapter_path", "progression_proof", "scale_tier_id"}
)
_CROSS_TIER_RUNTIME_FIELDS = (
    "model_dir_manifest_sha256",
    "trainer_artifact_id",
    "trainer_deployment_sha256",
    "trainer_implementation_sha256",
    "training_runtime_manifest_sha256",
)


class ProofError(RuntimeError):
    """Observed Windows scale-progression qualification is invalid."""


def _fail(message: str) -> NoReturn:
    raise ProofError(message)


def _strict_object(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> NoReturn:
    _fail(f"non-finite JSON constant: {value}")


def _read_object_snapshot(path: Path) -> tuple[dict[str, object], bytes]:
    try:
        raw = _stable_file_bytes(
            path,
            max_bytes=_MAX_JSON_BYTES,
            name=f"JSON authority {path.name}",
        )
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except ProofError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProofError(f"invalid JSON authority: {path.name}") from exc
    if type(value) is not dict:
        _fail(f"JSON authority must be an object: {path.name}")
    return value, raw


def _read_object(path: Path) -> dict[str, object]:
    value, _ = _read_object_snapshot(path)
    return value


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _replace_json(path: Path, value: object) -> None:
    temporary = path.with_name(path.name + ".tmp")
    if temporary.exists():
        _fail(f"stale temporary file exists: {temporary.name}")
    payload = (
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _write_new(path: Path, payload: bytes) -> None:
    if not payload:
        _fail(f"empty evidence payload: {path.name}")
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise ProofError(f"evidence already exists or cannot be written: {path.name}") from exc


def _sha256_file(path: Path) -> tuple[str, int]:
    """Hash one stable single-link regular-file authority through a held descriptor."""

    descriptor: int | None = None
    try:
        before = os.lstat(path)
        if (
            stat.S_ISLNK(before.st_mode)
            or _is_reparse(before)
            or not stat.S_ISREG(before.st_mode)
            or int(getattr(before, "st_nlink", 1)) != 1
            or before.st_size <= 0
        ):
            _fail(f"authority file type is invalid: {path.name}")
        descriptor = _open_readonly_snapshot(path)
        _require_open_snapshot_identity(
            path,
            descriptor,
            name=f"authority {path.name}",
        )
        opened = os.fstat(descriptor)
        os.lseek(descriptor, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        total = 0
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
            total += len(chunk)
        after = os.fstat(descriptor)
        current = os.lstat(path)
    except ProofError:
        raise
    except OSError as exc:
        raise ProofError(f"cannot hash authority: {path.name}") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass

    identities = tuple(
        _snapshot_identity(value)
        for value in (before, opened, after, current)
    )
    if len(set(identities)) != 1 or total != before.st_size:
        _fail(f"authority changed while hashing: {path.name}")
    return digest.hexdigest(), total


def _is_reparse(value: os.stat_result) -> bool:
    attributes = int(getattr(value, "st_file_attributes", 0))
    flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & flag)


def _snapshot_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(getattr(value, "st_ctime_ns", 0)),
        int(getattr(value, "st_nlink", 1)),
    )


def _open_readonly_snapshot(path: Path) -> int:
    if os.name == "nt":
        try:
            import ctypes
            import msvcrt
        except ImportError as exc:
            raise OSError("Windows evidence snapshot support is unavailable") from exc

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_void_p,
        ]
        create_file.restype = ctypes.c_void_p
        handle = create_file(
            os.fspath(path),
            _WINDOWS_GENERIC_READ,
            _WINDOWS_FILE_SHARE_READ,
            None,
            _WINDOWS_OPEN_EXISTING,
            _WINDOWS_FILE_ATTRIBUTE_NORMAL | _WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if handle is None or handle == invalid_handle:
            raise OSError(ctypes.get_last_error(), "CreateFileW failed")
        try:
            return msvcrt.open_osfhandle(
                int(handle),
                os.O_RDONLY | int(getattr(os, "O_BINARY", 0)),
            )
        except (OSError, OverflowError, ValueError):
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = [ctypes.c_void_p]
            close_handle.restype = ctypes.c_int
            close_handle(ctypes.c_void_p(handle))
            raise

    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise OSError("POSIX no-follow evidence snapshot support is unavailable")
    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    flags |= int(nofollow)
    flags |= int(getattr(os, "O_NONBLOCK", 0))
    return os.open(path, flags)


def _require_open_snapshot_identity(path: Path, descriptor: int, *, name: str) -> None:
    try:
        opened = os.fstat(descriptor)
        current = os.lstat(path)
    except OSError as exc:
        raise ProofError(f"{name} identity could not be verified") from exc
    if (
        not stat.S_ISREG(opened.st_mode)
        or int(getattr(opened, "st_nlink", 1)) != 1
        or stat.S_ISLNK(current.st_mode)
        or _is_reparse(current)
        or not stat.S_ISREG(current.st_mode)
        or int(getattr(current, "st_nlink", 1)) != 1
        or (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino)
    ):
        _fail(f"{name} identity changed")


def _read_held_bytes(
    descriptor: int,
    *,
    max_bytes: int,
    name: str,
) -> bytes:
    if type(max_bytes) is not int or max_bytes <= 0:
        _fail(f"{name} size bound is invalid")
    try:
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        total = 0
        while True:
            remaining = max_bytes + 1 - total
            if remaining <= 0:
                _fail(f"{name} size is invalid")
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                _fail(f"{name} size is invalid")
            chunks.append(chunk)
    except ProofError:
        raise
    except OSError as exc:
        raise ProofError(f"{name} could not be read") from exc
    if total <= 0:
        _fail(f"{name} is empty")
    return b"".join(chunks)


def _stable_file_bytes(path: Path, *, max_bytes: int, name: str) -> bytes:
    descriptor: int | None = None
    try:
        before = os.lstat(path)
        if (
            stat.S_ISLNK(before.st_mode)
            or _is_reparse(before)
            or not stat.S_ISREG(before.st_mode)
            or int(getattr(before, "st_nlink", 1)) != 1
            or before.st_size <= 0
            or before.st_size > max_bytes
        ):
            _fail(f"{name} size or file type is invalid")
        descriptor = _open_readonly_snapshot(path)
        _require_open_snapshot_identity(path, descriptor, name=name)
        opened = os.fstat(descriptor)
        payload = _read_held_bytes(
            descriptor,
            max_bytes=max_bytes,
            name=name,
        )
        after = os.fstat(descriptor)
        current = os.lstat(path)
    except ProofError:
        raise
    except OSError as exc:
        raise ProofError(f"{name} could not be snapshotted") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass

    identities = tuple(
        _snapshot_identity(value)
        for value in (before, opened, after, current)
    )
    if len(set(identities)) != 1 or len(payload) != before.st_size:
        _fail(f"{name} changed while it was being snapshotted")
    return payload


def _frozen_package_snapshot(
    path: Path,
    *,
    expected_manifest_sha256: str,
) -> tuple[FrozenLearningPackage, bytes]:
    raw = _stable_file_bytes(
        path,
        max_bytes=_MAX_JSON_BYTES,
        name=f"frozen learning package {path.name}",
    )
    try:
        package = FrozenLearningPackage.from_json(
            raw,
            expected_manifest_sha256=expected_manifest_sha256,
        )
    except (RuntimeError, TypeError, UnicodeError, ValueError) as exc:
        raise ProofError(f"frozen learning package is invalid: {path.name}") from exc
    return package, raw


def _load_frozen_package_snapshot(
    path: Path,
    *,
    expected_manifest_sha256: str,
) -> FrozenLearningPackage:
    package, _ = _frozen_package_snapshot(
        path,
        expected_manifest_sha256=expected_manifest_sha256,
    )
    return package


def _candidate_manifest_from_payload(
    payload: bytes,
    *,
    name: str,
) -> dict[str, object]:
    """Parse a candidate manifest from a private byte-exact snapshot."""

    if type(payload) is not bytes or not payload:
        _fail(f"{name} private manifest snapshot is invalid")
    descriptor: int | None = None
    try:
        with tempfile.TemporaryDirectory(prefix=".nika-peft-scale-candidate-") as snapshot_root:
            snapshot_path = Path(snapshot_root) / "adapter_model.safetensors"
            _write_new(snapshot_path, payload)
            before = os.lstat(snapshot_path)
            descriptor = _open_readonly_snapshot(snapshot_path)
            _require_open_snapshot_identity(
                snapshot_path,
                descriptor,
                name=f"{name} private manifest snapshot",
            )
            opened = os.fstat(descriptor)
            if (
                _read_held_bytes(
                    descriptor,
                    max_bytes=_MAX_CANDIDATE_BYTES,
                    name=f"{name} private manifest snapshot",
                )
                != payload
            ):
                _fail(f"{name} private manifest snapshot changed before parse")
            manifest = candidate_adapter_manifest(snapshot_path.resolve(strict=True))
            _require_open_snapshot_identity(
                snapshot_path,
                descriptor,
                name=f"{name} private manifest snapshot",
            )
            after = os.fstat(descriptor)
            current = os.lstat(snapshot_path)
            if (
                _read_held_bytes(
                    descriptor,
                    max_bytes=_MAX_CANDIDATE_BYTES,
                    name=f"{name} private manifest snapshot",
                )
                != payload
            ):
                _fail(f"{name} private manifest snapshot changed during parse")
            identities = tuple(
                _snapshot_identity(value)
                for value in (before, opened, after, current)
            )
            if len(set(identities)) != 1:
                _fail(f"{name} private manifest snapshot metadata changed during parse")
            return manifest
    except ProofError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ProofError(f"{name} private manifest snapshot could not be verified") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _candidate_file_authority(
    path: Path,
    *,
    name: str,
) -> tuple[str, int, dict[str, object]]:
    """Bind one candidate digest and manifest parse to one held file authority."""

    descriptor: int | None = None
    try:
        before = os.lstat(path)
        if (
            stat.S_ISLNK(before.st_mode)
            or _is_reparse(before)
            or not stat.S_ISREG(before.st_mode)
            or int(getattr(before, "st_nlink", 1)) != 1
            or before.st_size <= 0
            or before.st_size > _MAX_CANDIDATE_BYTES
        ):
            _fail(f"{name} size or file type is invalid")
        descriptor = _open_readonly_snapshot(path)
        _require_open_snapshot_identity(path, descriptor, name=name)
        opened = os.fstat(descriptor)
        payload = _read_held_bytes(
            descriptor,
            max_bytes=_MAX_CANDIDATE_BYTES,
            name=name,
        )
        if len(payload) != before.st_size:
            _fail(f"{name} changed while it was being read")

        manifest = _candidate_manifest_from_payload(payload, name=name)

        _require_open_snapshot_identity(path, descriptor, name=name)
        after = os.fstat(descriptor)
        current = os.lstat(path)
        if (
            _read_held_bytes(
                descriptor,
                max_bytes=_MAX_CANDIDATE_BYTES,
                name=name,
            )
            != payload
        ):
            _fail(f"{name} changed during manifest verification")
        identities = tuple(
            _snapshot_identity(value)
            for value in (before, opened, after, current)
        )
        if len(set(identities)) != 1:
            _fail(f"{name} identity changed during manifest verification")
    except ProofError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ProofError(f"{name} manifest authority could not be verified") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    return hashlib.sha256(payload).hexdigest(), len(payload), manifest


def _candidate_manifest_sha256(manifest: dict[str, object]) -> str:
    try:
        encoded = _canonical_json(manifest).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ProofError("candidate manifest is not canonical JSON evidence") from exc
    if not encoded or len(encoded) > _MAX_JSON_BYTES:
        _fail("candidate manifest has invalid canonical size")
    return hashlib.sha256(encoded).hexdigest()


def _candidate_training_digests(
    manifest: dict[str, object],
    *,
    name: str,
) -> tuple[str, str, str]:
    values: list[str] = []
    for field in (
        "previous_adapter_tensors_sha256",
        "trained_adapter_tensors_sha256",
        "tokenization_sha256",
    ):
        value = manifest.get(field)
        if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
            _fail(f"{name} lacks canonical {field}")
        values.append(value)
    previous, trained, tokenization = values
    if previous == trained:
        _fail(f"{name} does not prove adapter tensor mutation")
    return previous, trained, tokenization


def _material_limits(
    package: FrozenLearningPackage,
) -> tuple[int, int, int, int]:
    training_records = 0
    training_bytes = 0
    validation_records = 0
    validation_bytes = 0
    for shard in package.shards:
        if shard.split is LearningDataSplit.TRAINING:
            training_records += shard.record_count
            training_bytes += shard.byte_count
        elif shard.split is LearningDataSplit.VALIDATION:
            validation_records += shard.record_count
            validation_bytes += shard.byte_count
    if min(
        training_records,
        training_bytes,
        validation_records,
        validation_bytes,
    ) <= 0:
        _fail("frozen package lacks bounded train/validation material")
    return (
        training_records,
        training_bytes,
        validation_records,
        validation_bytes,
    )


def _evaluation_policy(*, minimum_improvement: float) -> dict[str, object]:
    return {
        "primary_metric": "model_quality_score",
        "minimum_improvement": minimum_improvement,
        "minimum_replays": 1,
        "primary_higher_is_better": True,
        "guardrails": [
            {
                "metric": "model_task_pass",
                "higher_is_better": True,
                "max_regression": 1.0,
            }
        ],
    }


def _require_scale_promotion_config(config: dict[str, object]) -> None:
    if (
        config.get("experiment_id") != _EXPERIMENT_ID
        or config.get("policy") != _evaluation_policy(minimum_improvement=0.0)
    ):
        _fail("physical evaluation config is not the exact scale promotion policy")


def _require_tier1_config_continuity(
    tier0: dict[str, object],
    tier1: dict[str, object],
) -> None:
    expected_keys = set(tier0) | set(_TIER1_ADDED_CONFIG_FIELDS)
    if set(tier1) != expected_keys:
        _fail("tier-1 config fields changed outside the scale transition")
    for key, value in tier0.items():
        if key in _TIER1_REPLACED_CONFIG_FIELDS:
            continue
        if tier1.get(key) != value:
            _fail(f"tier-1 config changed preserved field: {key}")


def _tier1_candidate_descriptor() -> dict[str, str]:
    return {
        "model_id": "nika-physical-scale-tier1-adapter",
        "source_reference": "project-internal:physical-scale-progression-proof",
        "license_reference": "project-internal:Nika-Core",
    }


def _require_tier1_transition_values(
    root: Path,
    tier1: dict[str, object],
    *,
    tier0: PhysicalTrainingPilotReport,
    proof: object,
) -> Path:
    try:
        output_root = Path(str(tier1.get("output_root", ""))).resolve(strict=True)
        initial_adapter = Path(
            str(tier1.get("initial_adapter_path", ""))
        ).resolve(strict=True)
        expected_adapter = candidate_artifact_path(
            root / "run",
            tier0.candidate_artifact_ref,
        ).resolve(strict=True)
    except OSError as exc:
        raise ProofError("tier-1 transition paths are unavailable") from exc
    if (
        tier1.get("schema_version") != 3
        or tier1.get("job_id") != _TIER1_JOB_ID
        or tier1.get("base_artifact_ref") != tier0.candidate_artifact_ref
        or tier1.get("output_root") != str(output_root)
        or output_root != (root / "tier1-run").resolve(strict=True)
        or tier1.get("candidate_artifact_ref") != _TIER1_CANDIDATE_REF
        or tier1.get("candidate_descriptor") != _tier1_candidate_descriptor()
        or tier1.get("scale_tier_id") != _TIER1_ID
        or tier1.get("progression_proof") != proof.canonical_payload()
        or initial_adapter != expected_adapter
        or tier1.get("initial_adapter_path") != str(initial_adapter)
    ):
        _fail("tier-1 config does not encode the exact scale transition")
    return initial_adapter


def _cross_tier_runtime_authority(
    tier0: PhysicalTrainingPilotReport,
    tier1: PhysicalTrainingPilotReport,
) -> dict[str, str]:
    result: dict[str, str] = {}
    for field in _CROSS_TIER_RUNTIME_FIELDS:
        first = getattr(tier0, field)
        second = getattr(tier1, field)
        if (
            type(first) is not str
            or _SHA256_RE.fullmatch(first) is None
            or second != first
        ):
            _fail(f"cross-tier runtime authority changed: {field}")
        result[field] = first
    return result


def _require_evaluation_binding(
    evaluation: dict[str, object],
    *,
    tier0: PhysicalTrainingPilotReport,
    proof: object,
    evaluation_set_sha256: str,
    previous_champion_id: object,
) -> None:
    if (
        evaluation.get("requested_experiment_id") != _EXPERIMENT_ID
        or evaluation.get("experiment_status") != "promoted"
        or evaluation.get("selected_candidate_id") != tier0.candidate_artifact_ref
        or evaluation.get("previous_champion_id") != previous_champion_id
        or evaluation.get("physical_pilot_evidence_sha256") != tier0.evidence_sha256
        or evaluation.get("evaluation_set_sha256") != evaluation_set_sha256
        or evaluation.get("comparison_evidence_sha256")
        != getattr(proof, "comparison_evidence_sha256", None)
        or getattr(proof, "candidate_artifact_ref", None)
        != tier0.candidate_artifact_ref
        or getattr(proof, "candidate_sha256", None) != tier0.candidate_sha256
        or getattr(proof, "execution_plan_sha256", None)
        != tier0.execution_plan_sha256
        or getattr(proof, "evaluation_set_sha256", None)
        != evaluation_set_sha256
    ):
        _fail("old-vs-new evaluation evidence does not bind exact tier-0 authority")


def _scale_plan_payload(package: FrozenLearningPackage) -> dict[str, object]:
    train_records, train_bytes, validation_records, validation_bytes = (
        _material_limits(package)
    )
    common = {
        "max_training_records": train_records,
        "max_training_bytes": train_bytes,
        "max_validation_records": validation_records,
        "max_validation_bytes": validation_bytes,
    }
    return {
        "plan_id": "physical-real-scale-progression",
        "tiers": [
            {**common, "max_steps": 2, "tier_id": _TIER0_ID},
            {**common, "max_steps": _TIER1_STEPS, "tier_id": _TIER1_ID},
        ],
    }


def prepare_tier0(root: Path) -> None:
    root = root.resolve(strict=True)
    config_path = root / "physical-pilot.json"
    package_path = root / "frozen-package.json"
    config = _read_object(config_path)
    if config.get("schema_version") != 1 or "scale_plan" in config:
        _fail("tier-0 proof config must start from canonical schema-v1 preparation")
    package = _load_frozen_package_snapshot(
        package_path,
        expected_manifest_sha256=str(config.get("frozen_package_sha256", "")),
    )
    config["schema_version"] = 2
    config["scale_plan"] = _scale_plan_payload(package)
    _replace_json(config_path, config)
    print(config_path)


def configure_promotion(root: Path) -> None:
    root = root.resolve(strict=True)
    path = root / "physical-evaluation.json"
    config = _read_object(path)
    policy = config.get("policy")
    if policy != _evaluation_policy(minimum_improvement=0.000001):
        _fail("physical evaluation policy is not the canonical real-proof policy")
    config["policy"] = _evaluation_policy(minimum_improvement=0.0)
    config["experiment_id"] = _EXPERIMENT_ID
    _replace_json(path, config)
    print(path)


def _pilot_report(root: Path) -> PhysicalTrainingPilotReport:
    try:
        _value, raw = _read_object_snapshot(root / "physical-pilot-report.json")
        return PhysicalTrainingPilotReport.from_json(
            raw.decode("utf-8", errors="strict")
        )
    except ProofError:
        raise
    except (RuntimeError, TypeError, UnicodeError, ValueError) as exc:
        raise ProofError("physical pilot report is invalid") from exc


def _trusted_progression(
    output_root: Path,
    *,
    workspace_id: str,
    job_id: str,
) -> object:
    if type(job_id) is not str or not job_id:
        _fail("tier-0 job identity is invalid")
    store = SQLiteStore(output_root / "physical-pilot.sqlite3")
    store.initialize()
    task = _find_pilot_task(
        store,
        workspace_id=workspace_id,
        job_id=job_id,
    )
    ledger = IdempotencyLedger(store)
    claim: dict[str, object] | None = None
    for record in _iter_task_idempotency_records(
        ledger,
        task_id=task.task_id,
    ):
        if (
            record.status is not IdempotencyStatus.COMPLETED
            or record.operation_type != _SCALE_PROGRESSION_OPERATION_TYPE
        ):
            continue
        result = record.result
        if (
            type(result) is dict
            and result.get("schema")
            == "nika-physical-scale-progression-record-v1"
            and type(result.get("proof")) is dict
        ):
            if claim is not None:
                _fail("exactly one durable scale progression proof is required")
            claim = dict(result["proof"])
    if claim is None:
        _fail("exactly one durable scale progression proof is required")
    proof = load_trusted_scale_progression_proof(
        output_root,
        workspace_id=workspace_id,
        expected_claim=claim,
    )
    if proof.canonical_payload() != claim:
        _fail("restored scale progression proof changed canonical payload")
    return proof


def prepare_tier1(root: Path) -> None:
    root = root.resolve(strict=True)
    tier0_config = _read_object(root / "physical-pilot.json")
    if tier0_config.get("schema_version") != 2:
        _fail("tier-0 scale config must use schema version 2")
    workspace_id = tier0_config.get("workspace_id")
    if type(workspace_id) is not str or not workspace_id:
        _fail("tier-0 workspace identity is invalid")

    tier0_root = root / "run"
    report = _pilot_report(tier0_root)
    _require_scale_promotion_config(
        _read_object(root / "physical-evaluation.json")
    )
    evaluation = _read_object(
        tier0_root / "physical-old-new-evaluation-report.json"
    )

    proof = _trusted_progression(
        tier0_root,
        workspace_id=workspace_id,
        job_id=report.job_id,
    )
    if (
        proof.tier_index != 0
        or proof.candidate_artifact_ref != report.candidate_artifact_ref
        or proof.candidate_sha256 != report.candidate_sha256
    ):
        _fail("durable progression proof does not bind the tier-0 candidate")

    initial_adapter = candidate_artifact_path(
        tier0_root,
        report.candidate_artifact_ref,
    ).resolve(strict=True)
    adapter_sha256, adapter_size, _ = _candidate_file_authority(
        initial_adapter,
        name="tier-0 promoted adapter",
    )
    if (
        adapter_sha256 != report.candidate_sha256
        or adapter_size != report.candidate_byte_count
    ):
        _fail("promoted adapter bytes do not match tier-0 report")

    source_package = _load_frozen_package_snapshot(
        root / "frozen-package.json",
        expected_manifest_sha256=str(
            tier0_config.get("frozen_package_sha256", "")
        ),
    )
    _require_evaluation_binding(
        evaluation,
        tier0=report,
        proof=proof,
        evaluation_set_sha256=source_package.evaluation_set_sha256,
        previous_champion_id=tier0_config.get("base_artifact_ref"),
    )
    tier1_package = FrozenLearningPackage.freeze(
        package_id=source_package.package_id,
        package_version="2",
        base_artifact_sha256=report.candidate_sha256,
        selection_policy_sha256=source_package.selection_policy_sha256,
        verification_sha256=source_package.verification_sha256,
        evaluation_set_sha256=source_package.evaluation_set_sha256,
        shards=source_package.shards,
    )
    package_path = root / "tier1-frozen-package.json"
    _write_new(package_path, tier1_package.to_json().encode("utf-8"))

    config = dict(tier0_config)
    config.update(
        {
            "schema_version": 3,
            "job_id": _TIER1_JOB_ID,
            "frozen_package_path": str(package_path.resolve(strict=True)),
            "frozen_package_sha256": tier1_package.manifest_sha256,
            "base_artifact_ref": report.candidate_artifact_ref,
            "output_root": str((root / "tier1-run").resolve()),
            "candidate_artifact_ref": _TIER1_CANDIDATE_REF,
            "candidate_descriptor": _tier1_candidate_descriptor(),
            "scale_tier_id": _TIER1_ID,
            "initial_adapter_path": str(initial_adapter),
            "progression_proof": proof.canonical_payload(),
        }
    )
    path = root / "tier1-physical-pilot.json"
    _write_new(
        path,
        (
            json.dumps(
                config,
                allow_nan=False,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode("utf-8"),
    )
    print(path)


def verify(root: Path) -> None:
    root = root.resolve(strict=True)
    source_sha = os.environ.get("NIKA_CANDIDATE_SHA", "")
    if _SHA40_RE.fullmatch(source_sha) is None:
        _fail("NIKA_CANDIDATE_SHA must identify the exact repository head")

    tier0_config = _read_object(root / "physical-pilot.json")
    workspace_id = tier0_config.get("workspace_id")
    if type(workspace_id) is not str or not workspace_id:
        _fail("tier-0 workspace identity is invalid")
    tier0_package_path = root / "frozen-package.json"
    tier0_package = _load_frozen_package_snapshot(
        tier0_package_path,
        expected_manifest_sha256=str(
            tier0_config.get("frozen_package_sha256", "")
        ),
    )
    expected_plan = _scale_plan_payload(tier0_package)
    if tier0_config.get("scale_plan") != expected_plan:
        _fail("tier-0 config does not preserve the exact proof scale plan")
    tier0 = _pilot_report(root / "run")
    if (
        tier0.completed_steps != 2
        or tier0.platform != "windows"
        or tier0.frozen_package_sha256 != tier0_package.manifest_sha256
    ):
        _fail("tier-0 evidence is not the real Windows two-step pilot")

    evaluation_config = _read_object(root / "physical-evaluation.json")
    _require_scale_promotion_config(evaluation_config)
    evaluation = _read_object(
        root / "run" / "physical-old-new-evaluation-report.json"
    )

    proof = _trusted_progression(
        root / "run",
        workspace_id=workspace_id,
        job_id=tier0.job_id,
    )
    if proof.tier_index != 0:
        _fail("restored progression authority does not bind tier 0")
    _require_evaluation_binding(
        evaluation,
        tier0=tier0,
        proof=proof,
        evaluation_set_sha256=tier0_package.evaluation_set_sha256,
        previous_champion_id=tier0_config.get("base_artifact_ref"),
    )

    tier1_config = _read_object(root / "tier1-physical-pilot.json")
    _require_tier1_config_continuity(tier0_config, tier1_config)
    if tier1_config.get("scale_plan") != expected_plan:
        _fail("tier-1 config does not preserve the exact scale plan")
    initial_adapter = _require_tier1_transition_values(
        root,
        tier1_config,
        tier0=tier0,
        proof=proof,
    )
    initial_sha256, initial_size, initial_manifest = _candidate_file_authority(
        initial_adapter,
        name="tier-0 promoted adapter",
    )
    if (
        initial_sha256 != tier0.candidate_sha256
        or initial_size != tier0.candidate_byte_count
    ):
        _fail("tier-1 warm-start adapter changed after promotion")
    base_gguf_sha256, _ = _sha256_file(root / "base.gguf")
    (
        tier0_previous_tensors_sha256,
        initial_tensors_sha256,
        tier0_tokenization_sha256,
    ) = _candidate_training_digests(
        initial_manifest,
        name="tier-0 promoted adapter",
    )
    if (
        _candidate_manifest_sha256(initial_manifest)
        != tier0.candidate_manifest_sha256
        or tier0_previous_tensors_sha256
        != tier0.previous_adapter_tensors_sha256
        or initial_tensors_sha256 != tier0.trained_adapter_tensors_sha256
        or initial_manifest.get("foundation_model_sha256") != base_gguf_sha256
    ):
        _fail("tier-0 candidate manifest does not match physical tensor evidence")

    tier1_root = root / "tier1-run"
    tier1 = _pilot_report(tier1_root)
    if (
        tier1.completed_steps != _TIER1_STEPS
        or tier1.job_id != _TIER1_JOB_ID
        or tier1.platform != "windows"
        or tier1.base_sha256 != tier0.candidate_sha256
        or tier1.candidate_artifact_ref != _TIER1_CANDIDATE_REF
        or tier1.previous_adapter_tensors_sha256
        != initial_tensors_sha256
        or tier1.previous_adapter_tensors_sha256
        == tier1.trained_adapter_tensors_sha256
    ):
        _fail("tier-1 physical report does not prove full-budget warm-start training")

    runtime_authority = _cross_tier_runtime_authority(tier0, tier1)

    tier1_package_path = Path(
        str(tier1_config.get("frozen_package_path", ""))
    ).resolve(strict=True)
    expected_tier1_package_path = (
        root / "tier1-frozen-package.json"
    ).resolve(strict=True)
    if tier1_package_path != expected_tier1_package_path:
        _fail("tier-1 config points at an unexpected frozen package")
    tier1_package, tier1_package_bytes = _frozen_package_snapshot(
        tier1_package_path,
        expected_manifest_sha256=tier1.frozen_package_sha256,
    )
    if (
        tier1_config.get("frozen_package_sha256")
        != tier1.frozen_package_sha256
        or tier1_package.base_artifact_sha256
        != tier0.candidate_sha256
        or tier1_package.evaluation_set_sha256
        != tier0_package.evaluation_set_sha256
        or tier1_package.candidate_dataset_sha256
        != tier0_package.candidate_dataset_sha256
    ):
        _fail("tier-1 frozen package does not preserve progression authority")

    store = SQLiteStore(tier1_root / "physical-pilot.sqlite3")
    store.initialize()
    task = _find_pilot_task(
        store,
        workspace_id=workspace_id,
        job_id=tier1.job_id,
    )
    payload = task.payload
    if (
        payload.get("scale_tier_id") != _TIER1_ID
        or payload.get("scale_plan_sha256") != proof.plan_sha256
        or payload.get("progression_proof") != proof.canonical_payload()
        or payload.get("progression_proof_sha256") != proof.proof_sha256
    ):
        _fail("tier-1 durable task does not bind the trusted progression proof")

    candidate = candidate_artifact_path(
        tier1_root,
        tier1.candidate_artifact_ref,
    ).resolve(strict=True)
    candidate_sha256, candidate_size, manifest = _candidate_file_authority(
        candidate,
        name="tier-1 candidate adapter",
    )
    if (
        candidate_sha256 != tier1.candidate_sha256
        or candidate_size != tier1.candidate_byte_count
        or candidate_sha256 == tier0.candidate_sha256
    ):
        _fail("tier-1 candidate bytes do not match physical evidence")
    (
        tier1_previous_tensors_sha256,
        tier1_trained_tensors_sha256,
        tier1_tokenization_sha256,
    ) = _candidate_training_digests(
        manifest,
        name="tier-1 candidate adapter",
    )
    if (
        manifest.get("schema") != "nika-peft-candidate-v3"
        or manifest.get("base_artifact_ref")
        != tier0.candidate_artifact_ref
        or manifest.get("candidate_artifact_ref") != _TIER1_CANDIDATE_REF
        or manifest.get("base_artifact_sha256") != tier0.candidate_sha256
        or tier1_previous_tensors_sha256 != initial_tensors_sha256
        or tier1_previous_tensors_sha256
        != tier1.previous_adapter_tensors_sha256
        or tier1_trained_tensors_sha256
        != tier1.trained_adapter_tensors_sha256
        or tier1_tokenization_sha256 != tier0_tokenization_sha256
        or _candidate_manifest_sha256(manifest)
        != tier1.candidate_manifest_sha256
        or manifest.get("foundation_model_sha256") != base_gguf_sha256
    ):
        _fail("tier-1 candidate manifest does not bind warm-start foundation authority")

    staged_assets_path = root / "staged-assets.json"
    staged_assets, staged_assets_bytes = _read_object_snapshot(
        staged_assets_path
    )
    staged_assets_sha256 = hashlib.sha256(staged_assets_bytes).hexdigest()

    evidence = root / "scale-evidence"
    try:
        evidence.mkdir()
    except OSError as exc:
        raise ProofError("scale evidence directory could not be created") from exc
    summary = {
        "schema": "nika-real-physical-scale-progression-proof-v3",
        "source_sha": source_sha,
        "platform": "windows",
        "promotion_policy": "non-regression",
        "minimum_improvement": 0.0,
        "quality_improvement_claimed": False,
        "scale_plan": expected_plan,
        "scale_plan_sha256": proof.plan_sha256,
        "evaluation_set_sha256": proof.evaluation_set_sha256,
        "comparison_evidence_sha256": proof.comparison_evidence_sha256,
        "model_dir_manifest_sha256": runtime_authority[
            "model_dir_manifest_sha256"
        ],
        "progression_proof": proof.canonical_payload(),
        "progression_proof_sha256": proof.proof_sha256,
        "staged_assets_sha256": staged_assets_sha256,
        "resource_budget": tier0_config.get("resource_budget"),
        "runtime_versions": tier0_config.get("runtime_versions"),
        "trainer_parameters": tier0_config.get("trainer_parameters"),
        "trainer_artifact_id": runtime_authority["trainer_artifact_id"],
        "trainer_deployment_sha256": runtime_authority[
            "trainer_deployment_sha256"
        ],
        "trainer_implementation_sha256": runtime_authority[
            "trainer_implementation_sha256"
        ],
        "training_runtime_manifest_sha256": runtime_authority[
            "training_runtime_manifest_sha256"
        ],
        "tier0_evidence_sha256": tier0.evidence_sha256,
        "tier0_frozen_package_sha256": tier0.frozen_package_sha256,
        "tier0_candidate_sha256": tier0.candidate_sha256,
        "tier0_candidate_manifest_sha256": tier0.candidate_manifest_sha256,
        "tier0_completed_steps": tier0.completed_steps,
        "tier0_previous_adapter_tensors_sha256": (
            tier0_previous_tensors_sha256
        ),
        "tier0_trained_adapter_tensors_sha256": initial_tensors_sha256,
        "tokenization_sha256": tier0_tokenization_sha256,
        "tier1_evidence_sha256": tier1.evidence_sha256,
        "tier1_frozen_package_sha256": tier1.frozen_package_sha256,
        "tier1_candidate_sha256": tier1.candidate_sha256,
        "tier1_candidate_manifest_sha256": tier1.candidate_manifest_sha256,
        "tier1_completed_steps": tier1.completed_steps,
        "tier1_previous_adapter_tensors_sha256": (
            tier1.previous_adapter_tensors_sha256
        ),
        "tier1_trained_adapter_tensors_sha256": (
            tier1.trained_adapter_tensors_sha256
        ),
    }
    _write_new(
        evidence / "scale-progression-summary.json",
        (_canonical_json(summary) + "\n").encode("utf-8"),
    )
    _write_new(
        evidence / "tier0-physical-pilot-report.json",
        (tier0.to_json() + "\n").encode("utf-8"),
    )
    _write_new(
        evidence / "tier1-physical-pilot-report.json",
        (tier1.to_json() + "\n").encode("utf-8"),
    )
    _write_new(
        evidence / "tier0-old-vs-new-evaluation-report.json",
        (_canonical_json(evaluation) + "\n").encode("utf-8"),
    )
    _write_new(
        evidence / "tier0-frozen-package.json",
        tier0_package.to_json().encode("utf-8"),
    )
    _write_new(
        evidence / "tier1-frozen-package.json",
        tier1_package_bytes,
    )
    _write_new(
        evidence / "staged-assets.json",
        staged_assets_bytes,
    )
    print(_canonical_json(summary))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compose and verify an observed Windows PEFT scale progression run."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("prepare-tier0", "configure-promotion", "prepare-tier1", "verify"):
        command = commands.add_parser(name)
        command.add_argument("--root", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare-tier0":
            prepare_tier0(args.root)
        elif args.command == "configure-promotion":
            configure_promotion(args.root)
        elif args.command == "prepare-tier1":
            prepare_tier1(args.root)
        else:
            verify(args.root)
    except (KeyError, OSError, ProofError, RuntimeError, TypeError, ValueError) as exc:
        print(f"physical scale progression proof failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
