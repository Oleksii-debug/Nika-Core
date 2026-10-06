from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path
from typing import NoReturn

from nika_core.learning_package import FrozenLearningPackage
from nika_core.model_engineering import EvaluationCase, EvaluationPurpose, EvaluationSet
from nika_core.model_gateway.contracts import ModelMessage, PrivacyClass
from nika_core.training_peft_worker import (
    candidate_adapter_manifest,
    candidate_artifact_path,
    model_directory_manifest_sha256,
)
from nika_core.training_physical_evaluation_driver import (
    PhysicalEvaluationDriverError,
    _comparison_evidence_sha256_from_report,
)
from nika_core.training_physical_pilot import PhysicalTrainingPilotReport

_MODEL_REPOSITORY = "amakhov/tiny-random-llama"
_MODEL_REVISION = "fbf68d33cf68a9d1d4b71b3d098ae82c8c14443b"
_MODEL_LICENSE = "Apache-2.0"
_BASE_ARTIFACT_REF = "models/amakhov-tiny-random-llama"
_MODEL_FILES = (
    "config.json",
    "generation_config.json",
    "model.safetensors",
    "special_tokens_map.json",
    "tokenizer.json",
    "tokenizer_config.json",
)
_GGUF_FILE = "gguf/tiny-random-f16.gguf"
_RUNTIME_PACKAGES = (
    "torch",
    "transformers",
    "peft",
    "accelerate",
    "gguf",
    "safetensors",
)
_MODEL_SOURCE_REFERENCE = (
    "https://huggingface.co/amakhov/tiny-random-llama/tree/" + _MODEL_REVISION
)
_MODEL_LICENSE_REFERENCE = "https://www.apache.org/licenses/LICENSE-2.0"
_EVALUATOR_REPOSITORY = "https://github.com/Oleksii-debug/Nika-Core"
_EVALUATOR_LICENSE_REFERENCE = "project-internal:Nika-Core"
_HELD_OUT_PROVENANCE = "generated:physical-old-new-proof-v1"
_HELD_OUT_LICENSE = "CC0-1.0"
_HELD_OUT_FILE = "held-out-evaluation.json"
_RUNTIME_FILE = "physical-evaluation-runtime.json"
_EVALUATION_CONFIG_FILE = "physical-evaluation.json"
_EVALUATION_REPORT_FILE = "physical-old-new-evaluation-report.json"
_EXPERIMENT_ID = "physical-old-new-real-proof-v1"
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_MAX_JSON_BYTES = 1024 * 1024
_MAX_EVIDENCE_REPORT_BYTES = 64 * 1024
_MAX_EVIDENCE_CANDIDATE_BYTES = 64 * 1024 * 1024
_MAX_EVIDENCE_MANIFEST_BYTES = 64 * 1024
_MAX_MODEL_ASSET_BYTES = 64 * 1024 * 1024
_EVALUATION_REPORT_KEYS = frozenset(
    {
        "schema_version",
        "schema",
        "physical_pilot_evidence_sha256",
        "requested_experiment_id",
        "evaluation_set_sha256",
        "execution_config_sha256",
        "comparison_evidence_sha256",
        "experiment_id",
        "experiment_status",
        "selected_candidate_id",
        "previous_champion_id",
        "training_binding_sha256",
        "champion_binding_sha256",
        "champion_benchmark_sha256",
        "challenger_benchmark_sha256",
        "attestor_id",
        "attestor_sha256",
        "champion_provider_manifest_sha256",
        "challenger_provider_manifest_sha256",
        "definition_sha256",
        "observations_sha256",
        "observation_count",
    }
)
_WINDOWS_GENERIC_READ = 0x80000000
_WINDOWS_FILE_SHARE_READ = 0x00000001
_WINDOWS_OPEN_EXISTING = 3
_WINDOWS_FILE_ATTRIBUTE_NORMAL = 0x00000080
_WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000


class ProofError(RuntimeError):
    """The observed real old-vs-new physical evaluation proof is invalid."""


def _fail(message: str) -> NoReturn:
    raise ProofError(message)


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON constant: {value}")


def _load_object_bytes(raw: bytes, *, name: str) -> dict[str, object]:
    if not raw or len(raw) > _MAX_JSON_BYTES:
        _fail(f"JSON authority has invalid size: {name}")
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise ProofError(f"invalid JSON authority: {name}") from exc
    if type(value) is not dict:
        _fail(f"JSON authority must be an object: {name}")
    return value


def _load_object(path: Path) -> dict[str, object]:
    return _load_object_bytes(path.read_bytes(), name=path.name)


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _atomic_write(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    if temporary.exists():
        _fail(f"stale temporary file exists: {temporary.name}")
    encoded = text.encode("utf-8", errors="strict")
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            total += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), total


def _is_reparse(value: os.stat_result) -> bool:
    attributes = int(getattr(value, "st_file_attributes", 0))
    flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & flag)


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

    flags = os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
    flags |= int(getattr(os, "O_NOFOLLOW", 0))
    flags |= int(getattr(os, "O_NONBLOCK", 0))
    return os.open(path, flags)


def _stable_file_bytes(path: Path, *, max_bytes: int, name: str) -> bytes:
    descriptor: int | None = None
    try:
        before = os.lstat(path)
        if (
            stat.S_ISLNK(before.st_mode)
            or _is_reparse(before)
            or not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > max_bytes
        ):
            _fail(f"{name} size or file type is invalid")
        descriptor = _open_readonly_snapshot(path)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            _fail(f"{name} changed before it was opened")
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
        after = os.fstat(descriptor)
        current = os.lstat(path)
    except ProofError:
        raise
    except OSError as exc:
        raise ProofError(f"{name} could not be read") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass

    identities = (
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns),
        (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns),
        (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns),
        (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns),
    )
    if (
        len(set(identities)) != 1
        or total != before.st_size
        or stat.S_ISLNK(current.st_mode)
        or _is_reparse(current)
        or not stat.S_ISREG(current.st_mode)
    ):
        _fail(f"{name} changed while it was being snapshotted")
    if total <= 0 or total > max_bytes:
        _fail(f"{name} size is invalid")
    return b"".join(chunks)


def _write_new_file(path: Path, payload: bytes) -> None:
    if type(payload) is not bytes or not payload:
        _fail("physical evaluation evidence payload is invalid")
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise ProofError("physical evaluation evidence could not be published") from exc


def _require_candidate_tokenization_sha256(manifest: dict[str, object]) -> str:
    value = manifest.get("tokenization_sha256")
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        _fail("physical evaluation candidate lacks canonical tokenization evidence")
    return value


def _verified_candidate_tokenization_from_snapshot(
    root: Path,
    candidate_bytes: bytes,
    *,
    pilot: PhysicalTrainingPilotReport,
) -> str:
    """Bind tokenization provenance to the exact snapshotted candidate tensor bytes."""

    if type(candidate_bytes) is not bytes or not candidate_bytes:
        _fail("physical evaluation candidate snapshot is invalid")
    try:
        with tempfile.TemporaryDirectory(
            prefix=".nika-peft-evaluation-candidate-",
            dir=root,
        ) as snapshot_root:
            snapshot_path = Path(snapshot_root) / "adapter_model.safetensors"
            _write_new_file(snapshot_path, candidate_bytes)
            descriptor: int | None = None
            try:
                descriptor = _open_readonly_snapshot(snapshot_path)
                if (
                    _stable_file_bytes(
                        snapshot_path,
                        max_bytes=_MAX_EVIDENCE_CANDIDATE_BYTES,
                        name="candidate verification snapshot",
                    )
                    != candidate_bytes
                ):
                    _fail("physical evaluation candidate snapshot changed before manifest read")
                manifest = candidate_adapter_manifest(snapshot_path.resolve(strict=True))
                if (
                    _stable_file_bytes(
                        snapshot_path,
                        max_bytes=_MAX_EVIDENCE_CANDIDATE_BYTES,
                        name="candidate verification snapshot",
                    )
                    != candidate_bytes
                ):
                    _fail("physical evaluation candidate snapshot changed during manifest read")
            finally:
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
    except ProofError:
        raise
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise ProofError(
            "physical evaluation candidate manifest could not be verified"
        ) from exc

    if manifest.get("schema") != "nika-peft-candidate-v2":
        _fail("old-vs-new proof requires a candidate-v2 tensor-evidence manifest")
    if manifest.get("candidate_artifact_ref") != pilot.candidate_artifact_ref:
        _fail("physical evaluation candidate logical reference changed")
    if (
        manifest.get("previous_adapter_tensors_sha256")
        != pilot.previous_adapter_tensors_sha256
    ):
        _fail("candidate previous tensor digest does not match pilot")
    if (
        manifest.get("trained_adapter_tensors_sha256")
        != pilot.trained_adapter_tensors_sha256
    ):
        _fail("candidate trained tensor digest does not match pilot")
    return _require_candidate_tokenization_sha256(manifest)


def _runtime_versions() -> dict[str, str]:
    result: dict[str, str] = {}
    for package in _RUNTIME_PACKAGES:
        installed = version(package)
        if not installed or installed != installed.strip():
            _fail(f"invalid installed runtime version: {package}")
        result[package] = installed
    return result


def _verified_pilot_config(
    root: Path,
    raw: bytes,
    *,
    pilot: PhysicalTrainingPilotReport,
) -> dict[str, object]:
    value = _load_object_bytes(raw, name="physical-pilot.json")
    required = {
        "schema_version": 1,
        "base_artifact_ref": _BASE_ARTIFACT_REF,
        "candidate_artifact_ref": pilot.candidate_artifact_ref,
        "frozen_package_sha256": pilot.frozen_package_sha256,
    }
    for key, expected in required.items():
        if value.get(key) != expected:
            _fail(f"physical pilot config has wrong {key}")

    runtime_versions = value.get("runtime_versions")
    if (
        type(runtime_versions) is not dict
        or set(runtime_versions) != set(_RUNTIME_PACKAGES)
        or runtime_versions != _runtime_versions()
    ):
        _fail("physical pilot runtime version authority changed")

    expected_paths = {
        "model_dir": (root / "model").resolve(strict=True),
        "base_gguf_path": (root / "base.gguf").resolve(strict=True),
        "output_root": (root / "run").resolve(strict=True),
    }
    for key, expected in expected_paths.items():
        raw_path = value.get(key)
        if type(raw_path) is not str:
            _fail(f"physical pilot config has invalid {key}")
        path = Path(raw_path)
        if not path.is_absolute():
            _fail(f"physical pilot config has non-absolute {key}")
        if path != expected:
            _fail(f"physical pilot config has non-canonical {key}")
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise ProofError(f"physical pilot config {key} is unavailable") from exc
        if resolved != expected:
            _fail(f"physical pilot config points at a different {key}")
    return value


def _verified_evaluation_report(
    value: dict[str, object],
    *,
    pilot: PhysicalTrainingPilotReport,
    evaluation_set_sha256: str,
    previous_champion_id: str,
) -> dict[str, object]:
    if frozenset(value) != _EVALUATION_REPORT_KEYS:
        _fail("physical evaluation report fields are invalid")
    if pilot.schema_version != 6 or pilot.platform != "windows":
        _fail("old-vs-new proof requires a fresh Windows schema-v6 pilot")
    if pilot.completed_steps != 2:
        _fail("old-vs-new proof requires the exact two-step pilot tier")

    required = {
        "schema_version": 2,
        "schema": "nika-physical-old-new-evaluation-report-v2",
        "physical_pilot_evidence_sha256": pilot.evidence_sha256,
        "requested_experiment_id": _EXPERIMENT_ID,
        "evaluation_set_sha256": evaluation_set_sha256,
        "selected_candidate_id": pilot.candidate_artifact_ref,
        "previous_champion_id": previous_champion_id,
    }
    for key, expected in required.items():
        if value.get(key) != expected:
            _fail(f"physical evaluation report has wrong {key}")

    status = value["experiment_status"]
    if status not in {"completed", "promoted"}:
        _fail("old-vs-new evaluation did not reach a canonical terminal state")
    observation_count = value["observation_count"]
    if type(observation_count) is not int or observation_count < 2:
        _fail("old-vs-new evaluation did not persist both model observations")

    for key in (
        "physical_pilot_evidence_sha256",
        "evaluation_set_sha256",
        "execution_config_sha256",
        "comparison_evidence_sha256",
        "training_binding_sha256",
        "champion_binding_sha256",
        "champion_benchmark_sha256",
        "challenger_benchmark_sha256",
        "attestor_sha256",
        "definition_sha256",
        "observations_sha256",
    ):
        digest = value[key]
        if (
            type(digest) is not str
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            _fail(f"physical evaluation report has invalid {key}")

    for key in (
        "champion_provider_manifest_sha256",
        "challenger_provider_manifest_sha256",
    ):
        digest = value[key]
        if digest is not None and (
            type(digest) is not str
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            _fail(f"physical evaluation report has invalid {key}")

    for key in ("experiment_id", "previous_champion_id", "attestor_id"):
        field = value[key]
        if type(field) is not str or not field or field != field.strip():
            _fail(f"physical evaluation report has invalid {key}")

    try:
        reproduced = _comparison_evidence_sha256_from_report(value)
    except (
        KeyError,
        PhysicalEvaluationDriverError,
        TypeError,
        ValueError,
    ) as exc:
        raise ProofError(
            "physical evaluation comparison evidence cannot be reconstructed"
        ) from exc
    if reproduced != value["comparison_evidence_sha256"]:
        _fail("physical evaluation comparison evidence digest is inconsistent")
    return value


def _verified_staged_assets(
    root: Path,
    raw: bytes,
    *,
    pilot: PhysicalTrainingPilotReport,
) -> dict[str, object]:
    value = _load_object_bytes(raw, name="staged-assets.json")
    expected_keys = {
        "license",
        "license_reference",
        "model_files",
        "repository",
        "revision",
        "runtime_versions",
        "source_reference",
    }
    if set(value) != expected_keys:
        _fail("physical evaluation staged asset manifest fields are invalid")
    if (
        value["repository"] != _MODEL_REPOSITORY
        or value["revision"] != _MODEL_REVISION
        or value["license"] != _MODEL_LICENSE
        or value["source_reference"] != _MODEL_SOURCE_REFERENCE
        or value["license_reference"] != _MODEL_LICENSE_REFERENCE
    ):
        _fail("physical evaluation staged asset provenance changed")

    runtime_versions = value["runtime_versions"]
    if (
        type(runtime_versions) is not dict
        or set(runtime_versions) != set(_RUNTIME_PACKAGES)
        or runtime_versions != _runtime_versions()
    ):
        _fail("physical evaluation runtime version evidence changed")

    model_dir = (root / "model").resolve(strict=True)
    try:
        current_model_manifest = model_directory_manifest_sha256(model_dir)
    except (OSError, TypeError, ValueError) as exc:
        raise ProofError("physical evaluation model directory is invalid") from exc
    if current_model_manifest != pilot.model_dir_manifest_sha256:
        _fail("physical evaluation model directory changed after the pilot")

    raw_files = value["model_files"]
    expected_paths = set(_MODEL_FILES) | {_GGUF_FILE}
    if type(raw_files) is not list or len(raw_files) != len(expected_paths):
        _fail("physical evaluation staged asset inventory is invalid")

    observed_paths: set[str] = set()
    for entry in raw_files:
        if type(entry) is not dict or set(entry) != {"path", "sha256", "size_bytes"}:
            _fail("physical evaluation staged asset entry is invalid")
        relative = entry["path"]
        digest = entry["sha256"]
        size_bytes = entry["size_bytes"]
        if (
            type(relative) is not str
            or relative not in expected_paths
            or relative in observed_paths
            or type(digest) is not str
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
            or type(size_bytes) is not int
            or size_bytes <= 0
            or size_bytes > _MAX_MODEL_ASSET_BYTES
        ):
            _fail("physical evaluation staged asset entry is invalid")

        source = root / "base.gguf" if relative == _GGUF_FILE else model_dir / relative
        payload = _stable_file_bytes(
            source,
            max_bytes=_MAX_MODEL_ASSET_BYTES,
            name=f"physical evaluation staged asset {relative}",
        )
        if len(payload) != size_bytes or _sha256_bytes(payload) != digest:
            _fail(f"physical evaluation staged asset identity changed: {relative}")
        if relative == _GGUF_FILE and digest != pilot.base_sha256:
            _fail("physical evaluation base GGUF changed after the pilot")
        observed_paths.add(relative)

    if observed_paths != expected_paths:
        _fail("physical evaluation staged asset inventory is incomplete")
    return value


def _evaluation_set() -> tuple[EvaluationSet, dict[str, object]]:
    case = EvaluationCase(
        case_id="held-out-physical-001",
        messages=(
            ModelMessage(
                role="user",
                content=(
                    "Held-out qualification prompt. Emit a short deterministic continuation "
                    "for the phrase: ultraviolet horizon"
                ),
            ),
        ),
        expected_text="held-out-reference-not-present-in-training",
        pass_score=1.0,
        weight=1.0,
    )
    evaluation = EvaluationSet(
        evaluation_set_id="physical-old-new-proof",
        version="1",
        provenance_ref=_HELD_OUT_PROVENANCE,
        license_ref=_HELD_OUT_LICENSE,
        purpose=EvaluationPurpose.HELD_OUT,
        privacy=PrivacyClass.PRIVATE,
        cases=(case,),
    )
    payload = {
        "evaluation_set_id": evaluation.evaluation_set_id,
        "version": evaluation.version,
        "provenance_ref": evaluation.provenance_ref,
        "license_ref": evaluation.license_ref,
        "purpose": evaluation.purpose.value,
        "privacy": evaluation.privacy.value,
        "cases": [
            {
                "case_id": case.case_id,
                "messages": [
                    {"role": message.role, "content": message.content}
                    for message in case.messages
                ],
                "expected_text": case.expected_text,
                "pass_score": float(case.pass_score),
                "weight": float(case.weight),
            }
        ],
    }
    return evaluation, payload


def prepare(root: Path) -> None:
    root = root.resolve(strict=True)
    pilot_config_path = root / "physical-pilot.json"
    package_path = root / "frozen-package.json"
    if not pilot_config_path.is_file() or not package_path.is_file():
        _fail("run physical_peft_real_proof.py prepare before evaluation preparation")

    pilot_config = _load_object(pilot_config_path)
    package = FrozenLearningPackage.from_json(package_path.read_bytes())
    configured_package = Path(str(pilot_config.get("frozen_package_path", "")))
    if configured_package.resolve(strict=True) != package_path:
        _fail("physical pilot config points at a different frozen package")

    evaluation, evaluation_payload = _evaluation_set()
    replacement = FrozenLearningPackage.freeze(
        package_id=package.package_id,
        package_version=package.package_version,
        base_artifact_sha256=package.base_artifact_sha256,
        selection_policy_sha256=package.selection_policy_sha256,
        verification_sha256=package.verification_sha256,
        evaluation_set_sha256=evaluation.content_sha256,
        shards=package.shards,
    )
    evaluation_path = root / _HELD_OUT_FILE
    if evaluation_path.exists():
        _fail("held-out evaluation authority already exists")
    _atomic_write(evaluation_path, _canonical_json(evaluation_payload) + "\n")
    _atomic_write(package_path, replacement.to_json())

    pilot_config["frozen_package_sha256"] = replacement.manifest_sha256
    _atomic_write(
        pilot_config_path,
        json.dumps(
            pilot_config,
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    prepared = {
        "evaluation_set_sha256": evaluation.content_sha256,
        "frozen_package_sha256": replacement.manifest_sha256,
        "model_repository": _MODEL_REPOSITORY,
        "model_revision": _MODEL_REVISION,
        "schema": "nika-physical-old-new-proof-preparation-v1",
    }
    _atomic_write(root / "evaluation-prepared.json", _canonical_json(prepared) + "\n")
    print(evaluation.content_sha256)


def configure(root: Path, evaluator_script: Path) -> None:
    root = root.resolve(strict=True)
    evaluator_script = evaluator_script.resolve(strict=True)
    if evaluator_script.suffix.casefold() != ".py":
        _fail("evaluator command file must be a Python source file")
    if os.name != "nt":
        _fail("physical evaluation configuration must be created on Windows")
    python_executable = Path(sys.executable).resolve(strict=True)
    if python_executable.suffix.casefold() != ".exe":
        _fail("physical evaluator executable must be a Windows .exe")

    pilot_config = _load_object(root / "physical-pilot.json")
    raw_report = (root / "run" / "physical-pilot-report.json").read_text(
        encoding="utf-8",
        errors="strict",
    )
    report = PhysicalTrainingPilotReport.from_json(raw_report)
    candidate = candidate_artifact_path(root / "run", report.candidate_artifact_ref)
    candidate = candidate.resolve(strict=True)
    candidate_sha256, candidate_size = _sha256_file(candidate)
    if (
        candidate_sha256 != report.candidate_sha256
        or candidate_size != report.candidate_byte_count
    ):
        _fail("trained candidate bytes do not match physical pilot evidence")

    evaluation, _ = _evaluation_set()
    package = FrozenLearningPackage.from_json(
        (root / "frozen-package.json").read_bytes(),
        expected_manifest_sha256=report.frozen_package_sha256,
    )
    if package.evaluation_set_sha256 != evaluation.content_sha256:
        _fail("physical pilot was not bound to the held-out evaluation authority")

    source_sha = os.environ.get("NIKA_CANDIDATE_SHA", "")
    if _SHA_RE.fullmatch(source_sha) is None:
        _fail("NIKA_CANDIDATE_SHA must identify the exact 40-hex repository head")
    evaluator_source = (
        f"{_EVALUATOR_REPOSITORY}/blob/{source_sha}/"
        "scripts/physical_peft_real_evaluator.py"
    )

    scratch_root = root / "evaluation-scratch"
    scratch_root.mkdir()
    runtime = {
        "schema_version": 1,
        "model_dir": os.fspath((root / "model").resolve(strict=True)),
        "model_dir_manifest_sha256": report.model_dir_manifest_sha256,
        "base_gguf_path": os.fspath((root / "base.gguf").resolve(strict=True)),
        "base_gguf_sha256": report.base_sha256,
        "scratch_root": os.fspath(scratch_root.resolve(strict=True)),
        "max_new_tokens": 4,
        "torch_num_threads": 2,
    }
    runtime_path = root / _RUNTIME_FILE
    if runtime_path.exists():
        _fail("physical evaluator runtime authority already exists")
    _atomic_write(runtime_path, _canonical_json(runtime) + "\n")

    descriptor = pilot_config.get("candidate_descriptor")
    if type(descriptor) is not dict:
        _fail("physical pilot config is missing candidate descriptor metadata")
    for field in ("model_id", "source_reference", "license_reference"):
        if type(descriptor.get(field)) is not str or not descriptor[field]:
            _fail(f"candidate descriptor is missing {field}")

    evaluation_config = {
        "schema_version": 1,
        "workspace_id": pilot_config["workspace_id"],
        "project_id": pilot_config["project_id"],
        "owner_id": pilot_config["owner_id"],
        "physical_pilot_output_root": os.fspath((root / "run").resolve(strict=True)),
        "frozen_package_path": os.fspath(
            (root / "frozen-package.json").resolve(strict=True)
        ),
        "base_artifact_ref": pilot_config["base_artifact_ref"],
        "base_model_path": os.fspath((root / "base.gguf").resolve(strict=True)),
        "candidate_model_path": os.fspath(candidate),
        "base_model": {
            "provider_id": "hf-gguf-local",
            "model_id": "amakhov-tiny-random-llama",
            "model_version": _MODEL_REVISION,
            "source_reference": _MODEL_SOURCE_REFERENCE,
            "license_reference": _MODEL_LICENSE_REFERENCE,
            "capabilities": ["text"],
        },
        "candidate_model": {
            "model_id": descriptor["model_id"],
            "source_reference": descriptor["source_reference"],
            "license_reference": descriptor["license_reference"],
        },
        "evaluator": {
            "executable": os.fspath(python_executable),
            "command_files": [
                os.fspath(evaluator_script),
                os.fspath(runtime_path.resolve(strict=True)),
            ],
            "switches": [],
            "provenance_ref": evaluator_source,
            "license_ref": _EVALUATOR_LICENSE_REFERENCE,
        },
        "evaluation_set_path": os.fspath((root / _HELD_OUT_FILE).resolve(strict=True)),
        "experiment_id": _EXPERIMENT_ID,
        "permission_fingerprint": "physical-evaluation-read-only-v1",
        "benchmark": {
            "timeout_seconds": 300.0,
            "temperature": 0.0,
            "scorer_id": "exact-match-nfc-v1",
        },
        "policy": {
            "primary_metric": "model_quality_score",
            "minimum_improvement": 0.000001,
            "minimum_replays": 1,
            "primary_higher_is_better": True,
            "guardrails": [
                {
                    "metric": "model_task_pass",
                    "higher_is_better": True,
                    "max_regression": 1.0,
                }
            ],
        },
    }
    config_path = root / _EVALUATION_CONFIG_FILE
    if config_path.exists():
        _fail("physical evaluation config already exists")
    _atomic_write(
        config_path,
        json.dumps(
            evaluation_config,
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
    )
    print(config_path)


def verify(root: Path) -> None:
    root = root.resolve(strict=True)
    pilot_report_path = root / "run" / "physical-pilot-report.json"
    evaluation_report_path = root / "run" / _EVALUATION_REPORT_FILE

    pilot_report_bytes = _stable_file_bytes(
        pilot_report_path,
        max_bytes=_MAX_EVIDENCE_REPORT_BYTES,
        name="physical pilot report",
    )
    report = PhysicalTrainingPilotReport.from_json(
        pilot_report_bytes.decode("utf-8", errors="strict")
    )
    pilot_config_bytes = _stable_file_bytes(
        root / "physical-pilot.json",
        max_bytes=_MAX_EVIDENCE_MANIFEST_BYTES,
        name="physical pilot config",
    )
    pilot_config = _verified_pilot_config(
        root,
        pilot_config_bytes,
        pilot=report,
    )
    evaluation_report_bytes = _stable_file_bytes(
        evaluation_report_path,
        max_bytes=_MAX_EVIDENCE_REPORT_BYTES,
        name="physical evaluation report",
    )
    evaluation_report = _load_object_bytes(
        evaluation_report_bytes,
        name=evaluation_report_path.name,
    )
    evaluation, _ = _evaluation_set()
    evaluation_report = _verified_evaluation_report(
        evaluation_report,
        pilot=report,
        evaluation_set_sha256=evaluation.content_sha256,
        previous_champion_id=str(pilot_config["base_artifact_ref"]),
    )
    status = evaluation_report["experiment_status"]
    observation_count = evaluation_report["observation_count"]
    assert type(status) is str
    assert type(observation_count) is int

    candidate = candidate_artifact_path(root / "run", report.candidate_artifact_ref)
    candidate = candidate.resolve(strict=True)
    candidate_bytes = _stable_file_bytes(
        candidate,
        max_bytes=_MAX_EVIDENCE_CANDIDATE_BYTES,
        name="physical evaluation candidate",
    )
    candidate_sha256 = _sha256_bytes(candidate_bytes)
    candidate_size = len(candidate_bytes)
    if (
        candidate_sha256 != report.candidate_sha256
        or candidate_size != report.candidate_byte_count
    ):
        _fail("candidate bytes changed after old-vs-new evaluation")
    tokenization_sha256 = _verified_candidate_tokenization_from_snapshot(
        root,
        candidate_bytes,
        pilot=report,
    )

    staged_assets_bytes = _stable_file_bytes(
        root / "staged-assets.json",
        max_bytes=_MAX_EVIDENCE_MANIFEST_BYTES,
        name="physical proof asset manifest",
    )
    staged_assets = _verified_staged_assets(
        root,
        staged_assets_bytes,
        pilot=report,
    )
    report_sha256 = _sha256_bytes(evaluation_report_bytes)
    source_sha = os.environ.get("NIKA_CANDIDATE_SHA", "")
    if _SHA_RE.fullmatch(source_sha) is None:
        _fail("NIKA_CANDIDATE_SHA must identify the exact proof source head")
    summary = {
        "attestor_id": evaluation_report["attestor_id"],
        "attestor_sha256": evaluation_report["attestor_sha256"],
        "candidate_sha256": candidate_sha256,
        "comparison_evidence_sha256": evaluation_report["comparison_evidence_sha256"],
        "evaluation_report_sha256": report_sha256,
        "evaluation_set_sha256": evaluation.content_sha256,
        "experiment_status": status,
        "model_repository": _MODEL_REPOSITORY,
        "model_revision": _MODEL_REVISION,
        "observation_count": observation_count,
        "physical_pilot_evidence_sha256": report.evidence_sha256,
        "proof_source_sha": source_sha,
        "model_dir_manifest_sha256": report.model_dir_manifest_sha256,
        "base_sha256": report.base_sha256,
        "base_artifact_ref": pilot_config["base_artifact_ref"],
        "staged_assets_sha256": _sha256_bytes(staged_assets_bytes),
        "asset_repository": staged_assets["repository"],
        "asset_revision": staged_assets["revision"],
        "tokenization_sha256": tokenization_sha256,
        "schema": "nika-physical-old-new-real-proof-v3",
    }

    evidence_dir = root / "evaluation-evidence"
    try:
        evidence_dir.mkdir()
    except OSError as exc:
        raise ProofError(
            "physical evaluation evidence directory could not be created"
        ) from exc
    _write_new_file(
        evidence_dir / "physical-pilot-report.json",
        pilot_report_bytes,
    )
    _write_new_file(
        evidence_dir / _EVALUATION_REPORT_FILE,
        evaluation_report_bytes,
    )
    _write_new_file(
        evidence_dir / "adapter_model.safetensors",
        candidate_bytes,
    )
    _write_new_file(
        evidence_dir / "staged-assets.json",
        staged_assets_bytes,
    )
    _atomic_write(
        evidence_dir / "physical-old-new-proof-summary.json",
        _canonical_json(summary) + "\n",
    )
    print(evaluation_report["comparison_evidence_sha256"])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prepare/configure/verify the real Windows old-vs-new PEFT proof."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--root", required=True, type=Path)
    configure_parser = commands.add_parser("configure")
    configure_parser.add_argument("--root", required=True, type=Path)
    configure_parser.add_argument("--evaluator-script", required=True, type=Path)
    verify_parser = commands.add_parser("verify")
    verify_parser.add_argument("--root", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "prepare":
            prepare(args.root)
        elif args.command == "configure":
            configure(args.root, args.evaluator_script)
        else:
            verify(args.root)
    except (
        KeyError,
        OSError,
        ProofError,
        RuntimeError,
        TypeError,
        ValueError,
    ) as exc:
        print(f"physical old-vs-new proof failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
