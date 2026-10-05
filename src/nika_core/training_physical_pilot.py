from __future__ import annotations

import hashlib
import hmac
import json
import os
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from nika_core.model_artifacts import ModelArtifactDescriptor
from nika_core.training_adapters import SubprocessTrainingWorker
from nika_core.training_artifacts import (
    CandidateArtifactIntegrityError,
    VerifiedCandidateArtifact,
    verify_candidate_artifact,
)
from nika_core.training_peft_worker import PeftTrainerError, candidate_adapter_manifest
from nika_core.training_runtime import (
    ArtifactIdentity,
    TrainingControl,
    TrainingJobSpec,
    TrainingRunEvidence,
    TrainingRunState,
    TrainingRuntime,
)
from nika_core.training_scale import TrainingScaleAuthorization

_SCHEMA_VERSION = 4
_REPORT_DOMAIN = b"nika-peft-physical-pilot-report-v4\x00"
_MAX_REPORT_BYTES = 32 * 1024
_MAX_CANDIDATE_MANIFEST_BYTES = 512 * 1024
_MAX_TEXT_BYTES = 1024
_MAX_STEPS = 1_000_000
_WINDOWS_GENERIC_READ = 0x80000000
_WINDOWS_FILE_SHARE_READ = 0x00000001
_WINDOWS_FILE_SHARE_WRITE = 0x00000002
_WINDOWS_OPEN_EXISTING = 3
_WINDOWS_FILE_ATTRIBUTE_NORMAL = 0x00000080
_WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_WINDOWS_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_PLATFORM_PATH_TYPE = type(Path())
_REQUIRED_REPORT_FIELDS = {
    "base_sha256",
    "candidate_artifact_ref",
    "candidate_byte_count",
    "candidate_descriptor_sha256",
    "candidate_registry_key",
    "candidate_sha256",
    "candidate_manifest_sha256",
    "consumed_materials_sha256",
    "model_dir_manifest_sha256",
    "trainer_artifact_id",
    "trainer_deployment_sha256",
    "trainer_implementation_sha256",
    "training_runtime_manifest_sha256",
    "completed_checkpoint_id",
    "completed_steps",
    "execution_plan_sha256",
    "frozen_package_sha256",
    "job_fingerprint",
    "trainer_job_fingerprint",
    "job_id",
    "paused_checkpoint_id",
    "restart_checkpoint_id",
    "platform",
    "scale_authorization_sha256",
    "schema_version",
    "training_material_sha256",
}


class PhysicalTrainingPilotError(RuntimeError):
    """Physical PEFT pilot evidence is absent, unsafe, or internally inconsistent."""


def _fail(message: str) -> NoReturn:
    raise PhysicalTrainingPilotError(message)


def _require_text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        _fail(f"{name} must be non-empty canonical text")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        _fail(f"{name} must be valid UTF-8 text")
    if len(encoded) > _MAX_TEXT_BYTES:
        _fail(f"{name} exceeds the configured byte limit")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        _fail(f"{name} must not contain control characters")
    return value


def _require_sha256(value: object, *, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        _fail(f"{name} must be an exact lowercase SHA-256 digest")
    return value


def _require_path(value: object, *, name: str) -> Path:
    if type(value) is not _PLATFORM_PATH_TYPE or not value.is_absolute():
        _fail(f"{name} must be an absolute canonical platform Path")
    return value


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_: str) -> NoReturn:
    raise ValueError("non-finite JSON constant")


def _canonical_json_bytes(value: object) -> bytes:
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise PhysicalTrainingPilotError("pilot report is not canonical JSON") from exc
    if len(encoded) > _MAX_REPORT_BYTES:
        _fail("pilot report exceeds the configured byte limit")
    return encoded


def _is_windows() -> bool:
    return os.name == "nt"


def _verify_candidate_receipt(
    *,
    candidate_path: Path,
    candidate_descriptor: ModelArtifactDescriptor,
    candidate_root: Path | None,
) -> VerifiedCandidateArtifact:
    path = _require_path(candidate_path, name="candidate_path")
    if type(candidate_descriptor) is not ModelArtifactDescriptor:
        raise TypeError("candidate_descriptor must be exact ModelArtifactDescriptor")
    if candidate_root is not None:
        root = _require_path(candidate_root, name="candidate_root")
    else:
        root = None
    try:
        receipt = verify_candidate_artifact(
            path,
            candidate_descriptor,
            allowed_root=root,
        )
    except (CandidateArtifactIntegrityError, TypeError, ValueError) as exc:
        raise PhysicalTrainingPilotError(
            "canonical candidate artifact verification failed"
        ) from exc
    if type(receipt) is not VerifiedCandidateArtifact:
        _fail("canonical candidate verifier returned invalid evidence")
    _require_sha256(receipt.descriptor_digest, name="candidate_descriptor_sha256")
    _require_sha256(receipt.registry_key, name="candidate_registry_key")
    _require_sha256(receipt.sha256, name="candidate_sha256")
    if (
        type(receipt.size_bytes) is not int
        or not 1 <= receipt.size_bytes <= (1 << 63) - 1
    ):
        _fail("canonical candidate verifier returned invalid size evidence")
    return receipt


@dataclass(frozen=True, slots=True)
class _CandidateManifestEvidence:
    candidate_manifest_sha256: str
    trainer_job_fingerprint: str
    consumed_materials_sha256: str
    model_dir_manifest_sha256: str
    trainer_artifact_id: str
    trainer_deployment_sha256: str
    trainer_implementation_sha256: str
    training_runtime_manifest_sha256: str


def _open_windows_candidate_stability_lock(path: Path) -> int | None:
    """Deny write/delete replacement while canonical byte + manifest checks run."""

    if os.name != "nt":
        return None
    try:
        import ctypes

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
            error_code = ctypes.get_last_error()
            raise OSError(error_code, "CreateFileW failed")
        return int(handle)
    except (AttributeError, ImportError, OSError, TypeError, ValueError) as exc:
        raise PhysicalTrainingPilotError(
            "candidate artifact could not be locked for stable verification"
        ) from exc


def _close_windows_candidate_stability_lock(handle: int | None) -> None:
    if handle is None:
        return
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int
        close_handle(ctypes.c_void_p(handle))
    except (AttributeError, ImportError, OSError, TypeError, ValueError):
        pass


def _open_windows_report_parent_stability_lock(
    path: Path,
    expected_snapshot: os.stat_result,
) -> int | None:
    """Deny parent-directory rename/delete while report publication is in flight."""

    if os.name != "nt":
        return None
    handle_value: int | None = None
    try:
        import ctypes

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
            0,
            _WINDOWS_FILE_SHARE_READ | _WINDOWS_FILE_SHARE_WRITE,
            None,
            _WINDOWS_OPEN_EXISTING,
            _WINDOWS_FILE_FLAG_BACKUP_SEMANTICS
            | _WINDOWS_FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if handle is None or handle == invalid_handle:
            error_code = ctypes.get_last_error()
            raise OSError(error_code, "CreateFileW failed")
        handle_value = int(handle)
        current = os.lstat(path)
        if (
            stat.S_ISLNK(current.st_mode)
            or _is_reparse_point(current)
            or not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino)
            != (expected_snapshot.st_dev, expected_snapshot.st_ino)
        ):
            raise OSError("report parent changed while acquiring stability lock")
        return handle_value
    except (AttributeError, ImportError, OSError, TypeError, ValueError) as exc:
        _close_windows_candidate_stability_lock(handle_value)
        raise PhysicalTrainingPilotError(
            "pilot report parent directory could not be locked for publication"
        ) from exc


def _candidate_manifest_evidence(
    *,
    candidate_path: Path,
    completed: TrainingRunEvidence,
    trainer_job_fingerprint: str,
    trainer_deployment_identity: ArtifactIdentity,
) -> _CandidateManifestEvidence:
    try:
        manifest = candidate_adapter_manifest(candidate_path)
    except (PeftTrainerError, OSError, TypeError, ValueError) as exc:
        raise PhysicalTrainingPilotError(
            "canonical PEFT candidate manifest verification failed"
        ) from exc
    if type(manifest) is not dict or manifest.get("schema") != "nika-peft-candidate-v1":
        _fail("canonical PEFT candidate manifest returned invalid evidence")

    if manifest.get("base_artifact_ref") != completed.base_artifact.artifact_ref:
        _fail("PEFT candidate manifest changed base artifact reference")
    base_sha256 = _require_sha256(
        manifest.get("base_artifact_sha256"),
        name="candidate manifest base_artifact_sha256",
    )
    if not hmac.compare_digest(base_sha256, completed.base_artifact.sha256):
        _fail("PEFT candidate manifest changed base artifact digest")
    if manifest.get("candidate_artifact_ref") != completed.candidate_artifact_ref:
        _fail("PEFT candidate manifest changed candidate artifact reference")
    manifest_job_fingerprint = _require_sha256(
        manifest.get("job_fingerprint"),
        name="candidate manifest job_fingerprint",
    )
    expected_trainer_job_fingerprint = _require_sha256(
        trainer_job_fingerprint,
        name="trainer_job_fingerprint",
    )
    if not hmac.compare_digest(
        manifest_job_fingerprint,
        expected_trainer_job_fingerprint,
    ):
        _fail("PEFT candidate manifest changed trainer protocol job fingerprint")
    step_number = manifest.get("step_number")
    if type(step_number) is not int or step_number != completed.next_step:
        _fail("PEFT candidate manifest does not match completed step boundary")

    consumed_materials_sha256 = _require_sha256(
        manifest.get("consumed_materials_sha256"),
        name="candidate manifest consumed_materials_sha256",
    )
    model_dir_manifest_sha256 = _require_sha256(
        manifest.get("model_dir_manifest_sha256"),
        name="candidate manifest model_dir_manifest_sha256",
    )
    if type(trainer_deployment_identity) is not ArtifactIdentity:
        raise TypeError("trainer_deployment_identity must be exact ArtifactIdentity")
    expected_trainer_artifact_id = _require_sha256(
        trainer_deployment_identity.artifact_ref,
        name="verified trainer artifact_id",
    )
    expected_trainer_deployment_sha256 = _require_sha256(
        trainer_deployment_identity.sha256,
        name="verified trainer sha256",
    )
    trainer_artifact_id = _require_sha256(
        manifest.get("trainer_artifact_id"),
        name="candidate manifest trainer_artifact_id",
    )
    trainer_deployment_sha256 = _require_sha256(
        manifest.get("trainer_sha256"),
        name="candidate manifest trainer_sha256",
    )
    if not hmac.compare_digest(trainer_artifact_id, expected_trainer_artifact_id):
        _fail("PEFT candidate manifest changed trainer artifact identity")
    if not hmac.compare_digest(
        trainer_deployment_sha256,
        expected_trainer_deployment_sha256,
    ):
        _fail("PEFT candidate manifest changed trainer deployment digest")
    trainer_implementation_sha256 = _require_sha256(
        manifest.get("trainer_implementation_sha256"),
        name="candidate manifest trainer_implementation_sha256",
    )
    training_runtime_manifest_sha256 = _require_sha256(
        manifest.get("training_runtime_manifest_sha256"),
        name="candidate manifest training_runtime_manifest_sha256",
    )
    try:
        encoded = json.dumps(
            manifest,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise PhysicalTrainingPilotError(
            "canonical PEFT candidate manifest could not be snapshotted"
        ) from exc
    if not encoded or len(encoded) > _MAX_CANDIDATE_MANIFEST_BYTES:
        _fail("canonical PEFT candidate manifest exceeds the evidence byte limit")

    return _CandidateManifestEvidence(
        candidate_manifest_sha256=hashlib.sha256(encoded).hexdigest(),
        trainer_job_fingerprint=expected_trainer_job_fingerprint,
        consumed_materials_sha256=consumed_materials_sha256,
        model_dir_manifest_sha256=model_dir_manifest_sha256,
        trainer_artifact_id=trainer_artifact_id,
        trainer_deployment_sha256=trainer_deployment_sha256,
        trainer_implementation_sha256=trainer_implementation_sha256,
        training_runtime_manifest_sha256=training_runtime_manifest_sha256,
    )


@dataclass(frozen=True, slots=True)
class PhysicalTrainingPilotReport:
    """Minimized, path-free evidence for one checkpoint/restart PEFT pilot."""

    job_id: str
    base_sha256: str
    frozen_package_sha256: str
    training_material_sha256: str
    scale_authorization_sha256: str
    execution_plan_sha256: str
    job_fingerprint: str
    trainer_job_fingerprint: str
    paused_checkpoint_id: str
    restart_checkpoint_id: str
    completed_checkpoint_id: str
    candidate_artifact_ref: str
    candidate_descriptor_sha256: str
    candidate_registry_key: str
    candidate_sha256: str
    candidate_byte_count: int
    candidate_manifest_sha256: str
    consumed_materials_sha256: str
    model_dir_manifest_sha256: str
    trainer_artifact_id: str
    trainer_deployment_sha256: str
    trainer_implementation_sha256: str
    training_runtime_manifest_sha256: str
    completed_steps: int
    platform: str = "windows"
    schema_version: int = _SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != _SCHEMA_VERSION:
            _fail("unsupported physical pilot report schema")
        if type(self.platform) is not str or self.platform != "windows":
            _fail("physical PEFT pilot report must identify Windows")
        for value, name in (
            (self.job_id, "job_id"),
            (self.paused_checkpoint_id, "paused_checkpoint_id"),
            (self.restart_checkpoint_id, "restart_checkpoint_id"),
            (self.completed_checkpoint_id, "completed_checkpoint_id"),
            (self.candidate_artifact_ref, "candidate_artifact_ref"),
        ):
            _require_text(value, name=name)
        for value, name in (
            (self.base_sha256, "base_sha256"),
            (self.frozen_package_sha256, "frozen_package_sha256"),
            (self.training_material_sha256, "training_material_sha256"),
            (self.scale_authorization_sha256, "scale_authorization_sha256"),
            (self.execution_plan_sha256, "execution_plan_sha256"),
            (self.job_fingerprint, "job_fingerprint"),
            (self.trainer_job_fingerprint, "trainer_job_fingerprint"),
            (self.candidate_descriptor_sha256, "candidate_descriptor_sha256"),
            (self.candidate_registry_key, "candidate_registry_key"),
            (self.candidate_sha256, "candidate_sha256"),
            (self.candidate_manifest_sha256, "candidate_manifest_sha256"),
            (self.consumed_materials_sha256, "consumed_materials_sha256"),
            (self.model_dir_manifest_sha256, "model_dir_manifest_sha256"),
            (self.trainer_artifact_id, "trainer_artifact_id"),
            (self.trainer_deployment_sha256, "trainer_deployment_sha256"),
            (self.trainer_implementation_sha256, "trainer_implementation_sha256"),
            (
                self.training_runtime_manifest_sha256,
                "training_runtime_manifest_sha256",
            ),
        ):
            _require_sha256(value, name=name)
        if len(
            {
                self.paused_checkpoint_id,
                self.restart_checkpoint_id,
                self.completed_checkpoint_id,
            }
        ) != 3:
            _fail("pause, restart probe, and completion need distinct durable checkpoints")
        if (
            type(self.candidate_byte_count) is not int
            or self.candidate_byte_count <= 0
            or self.candidate_byte_count > (1 << 63) - 1
        ):
            _fail("candidate_byte_count must be a positive signed-64 integer")
        if (
            type(self.completed_steps) is not int
            or self.completed_steps < 2
            or self.completed_steps > _MAX_STEPS
        ):
            _fail("physical pilot must complete after a real restart boundary")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "base_sha256": self.base_sha256,
            "candidate_artifact_ref": self.candidate_artifact_ref,
            "candidate_byte_count": self.candidate_byte_count,
            "candidate_descriptor_sha256": self.candidate_descriptor_sha256,
            "candidate_registry_key": self.candidate_registry_key,
            "candidate_sha256": self.candidate_sha256,
            "candidate_manifest_sha256": self.candidate_manifest_sha256,
            "consumed_materials_sha256": self.consumed_materials_sha256,
            "model_dir_manifest_sha256": self.model_dir_manifest_sha256,
            "trainer_artifact_id": self.trainer_artifact_id,
            "trainer_deployment_sha256": self.trainer_deployment_sha256,
            "trainer_implementation_sha256": self.trainer_implementation_sha256,
            "training_runtime_manifest_sha256": self.training_runtime_manifest_sha256,
            "completed_checkpoint_id": self.completed_checkpoint_id,
            "completed_steps": self.completed_steps,
            "execution_plan_sha256": self.execution_plan_sha256,
            "frozen_package_sha256": self.frozen_package_sha256,
            "job_fingerprint": self.job_fingerprint,
            "trainer_job_fingerprint": self.trainer_job_fingerprint,
            "job_id": self.job_id,
            "paused_checkpoint_id": self.paused_checkpoint_id,
            "restart_checkpoint_id": self.restart_checkpoint_id,
            "platform": self.platform,
            "scale_authorization_sha256": self.scale_authorization_sha256,
            "schema_version": self.schema_version,
            "training_material_sha256": self.training_material_sha256,
        }

    @property
    def evidence_sha256(self) -> str:
        return hashlib.sha256(
            _REPORT_DOMAIN + _canonical_json_bytes(self.canonical_payload())
        ).hexdigest()

    def to_json(self) -> str:
        return _canonical_json_bytes(self.canonical_payload()).decode("utf-8")

    @classmethod
    def from_json(cls, raw: str) -> PhysicalTrainingPilotReport:
        if type(raw) is not str:
            raise TypeError("physical pilot report JSON must be exact text")
        try:
            encoded = raw.encode("utf-8", errors="strict")
        except UnicodeEncodeError as exc:
            raise PhysicalTrainingPilotError("pilot report JSON must be valid UTF-8") from exc
        if not encoded or len(encoded) > _MAX_REPORT_BYTES:
            _fail("pilot report JSON is empty or too large")
        try:
            value = json.loads(
                raw,
                object_pairs_hook=_strict_object,
                parse_constant=_reject_constant,
            )
        except (json.JSONDecodeError, RecursionError, ValueError) as exc:
            raise PhysicalTrainingPilotError("pilot report JSON is invalid") from exc
        if type(value) is not dict or set(value) != _REQUIRED_REPORT_FIELDS:
            _fail("pilot report fields do not match the strict schema")
        return cls(
            job_id=value["job_id"],
            base_sha256=value["base_sha256"],
            frozen_package_sha256=value["frozen_package_sha256"],
            training_material_sha256=value["training_material_sha256"],
            scale_authorization_sha256=value["scale_authorization_sha256"],
            execution_plan_sha256=value["execution_plan_sha256"],
            job_fingerprint=value["job_fingerprint"],
            trainer_job_fingerprint=value["trainer_job_fingerprint"],
            paused_checkpoint_id=value["paused_checkpoint_id"],
            restart_checkpoint_id=value["restart_checkpoint_id"],
            completed_checkpoint_id=value["completed_checkpoint_id"],
            candidate_artifact_ref=value["candidate_artifact_ref"],
            candidate_descriptor_sha256=value["candidate_descriptor_sha256"],
            candidate_registry_key=value["candidate_registry_key"],
            candidate_sha256=value["candidate_sha256"],
            candidate_byte_count=value["candidate_byte_count"],
            candidate_manifest_sha256=value["candidate_manifest_sha256"],
            consumed_materials_sha256=value["consumed_materials_sha256"],
            model_dir_manifest_sha256=value["model_dir_manifest_sha256"],
            trainer_artifact_id=value["trainer_artifact_id"],
            trainer_deployment_sha256=value["trainer_deployment_sha256"],
            trainer_implementation_sha256=value["trainer_implementation_sha256"],
            training_runtime_manifest_sha256=value[
                "training_runtime_manifest_sha256"
            ],
            completed_steps=value["completed_steps"],
            platform=value["platform"],
            schema_version=value["schema_version"],
        )


def _is_reparse_point(snapshot: os.stat_result) -> bool:
    attributes = int(getattr(snapshot, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & reparse_flag)


def _canonical_report_output_path(value: object) -> tuple[Path, os.stat_result]:
    path = _require_path(value, name="report_path")
    parent = path.parent
    try:
        resolved_parent = parent.resolve(strict=True)
        parent_snapshot = os.lstat(parent)
    except OSError as exc:
        raise PhysicalTrainingPilotError(
            "pilot report parent directory is unavailable"
        ) from exc
    if resolved_parent != parent:
        _fail("pilot report parent directory must be canonical")
    if (
        stat.S_ISLNK(parent_snapshot.st_mode)
        or _is_reparse_point(parent_snapshot)
        or not stat.S_ISDIR(parent_snapshot.st_mode)
    ):
        _fail("pilot report parent directory must be a non-linked directory")
    try:
        os.lstat(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise PhysicalTrainingPilotError(
            "pilot report destination could not be inspected"
        ) from exc
    else:
        _fail("pilot report destination already exists")
    return path, parent_snapshot


def _unlink_published_report_if_owned(
    path: Path,
    expected_identity: tuple[int, int] | None,
) -> None:
    if expected_identity is None:
        return
    try:
        current = os.lstat(path)
    except OSError:
        return
    if (
        stat.S_ISLNK(current.st_mode)
        or _is_reparse_point(current)
        or not stat.S_ISREG(current.st_mode)
        or (current.st_dev, current.st_ino) != expected_identity
    ):
        return
    try:
        path.unlink()
    except OSError:
        pass


def write_physical_training_pilot_report(
    report: PhysicalTrainingPilotReport,
    report_path: Path,
) -> None:
    """Atomically publish one canonical physical-pilot report without clobbering evidence."""

    if type(report) is not PhysicalTrainingPilotReport:
        raise TypeError("report must be exact PhysicalTrainingPilotReport")
    destination, parent_before = _canonical_report_output_path(report_path)
    parent_lock = _open_windows_report_parent_stability_lock(
        destination.parent,
        parent_before,
    )
    payload = report.to_json().encode("utf-8")
    temporary: Path | None = None
    descriptor: int | None = None
    published_identity: tuple[int, int] | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
        )
        temporary = Path(temporary_name)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

        temporary_snapshot = os.lstat(temporary)
        if (
            stat.S_ISLNK(temporary_snapshot.st_mode)
            or _is_reparse_point(temporary_snapshot)
            or not stat.S_ISREG(temporary_snapshot.st_mode)
            or int(getattr(temporary_snapshot, "st_nlink", 1)) != 1
        ):
            _fail("pilot report temporary file is not a canonical regular file")
        published_identity = (
            int(temporary_snapshot.st_dev),
            int(temporary_snapshot.st_ino),
        )

        parent_during = os.lstat(destination.parent)
        if (
            stat.S_ISLNK(parent_during.st_mode)
            or _is_reparse_point(parent_during)
            or not stat.S_ISDIR(parent_during.st_mode)
            or (parent_during.st_dev, parent_during.st_ino)
            != (parent_before.st_dev, parent_before.st_ino)
        ):
            _fail("pilot report parent directory changed during publication")

        try:
            os.link(temporary, destination)
        except FileExistsError as exc:
            raise PhysicalTrainingPilotError(
                "pilot report destination already exists"
            ) from exc
        linked_snapshot = os.lstat(destination)
        if (
            stat.S_ISLNK(linked_snapshot.st_mode)
            or _is_reparse_point(linked_snapshot)
            or not stat.S_ISREG(linked_snapshot.st_mode)
            or (linked_snapshot.st_dev, linked_snapshot.st_ino)
            != published_identity
            or int(getattr(linked_snapshot, "st_nlink", 1)) != 2
        ):
            _fail("pilot report publication changed file identity")
        os.unlink(temporary)
        temporary = None

        final_snapshot = os.lstat(destination)
        if (
            stat.S_ISLNK(final_snapshot.st_mode)
            or _is_reparse_point(final_snapshot)
            or not stat.S_ISREG(final_snapshot.st_mode)
            or (final_snapshot.st_dev, final_snapshot.st_ino)
            != published_identity
            or int(getattr(final_snapshot, "st_nlink", 1)) != 1
        ):
            _fail("published pilot report is not a canonical regular file")
        try:
            published_payload = destination.read_bytes()
        except OSError as exc:
            raise PhysicalTrainingPilotError(
                "published pilot report could not be reverified"
            ) from exc
        if not hmac.compare_digest(published_payload, payload):
            _fail("published pilot report bytes changed during publication")
        try:
            published_text = published_payload.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise PhysicalTrainingPilotError(
                "published pilot report is not valid UTF-8"
            ) from exc
        restored = PhysicalTrainingPilotReport.from_json(published_text)
        if restored != report:
            _fail("published pilot report changed during parse-back verification")
        parent_after = os.lstat(destination.parent)
        if (
            stat.S_ISLNK(parent_after.st_mode)
            or _is_reparse_point(parent_after)
            or not stat.S_ISDIR(parent_after.st_mode)
            or (parent_after.st_dev, parent_after.st_ino)
            != (parent_before.st_dev, parent_before.st_ino)
        ):
            _fail("pilot report parent directory changed during publication")
    except PhysicalTrainingPilotError:
        _unlink_published_report_if_owned(destination, published_identity)
        raise
    except OSError as exc:
        _unlink_published_report_if_owned(destination, published_identity)
        raise PhysicalTrainingPilotError(
            "pilot report could not be published atomically"
        ) from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
        _close_windows_candidate_stability_lock(parent_lock)


def _snapshot_run_evidence(
    value: object,
    *,
    state: TrainingRunState,
    label: str,
) -> TrainingRunEvidence:
    if type(value) is not TrainingRunEvidence:
        _fail(f"{label} must be exact TrainingRunEvidence")
    try:
        if type(value.state) is not TrainingRunState or value.state is not state:
            _fail(f"{label} has an unexpected training state")
        if (
            type(value.next_step) is not int
            or value.next_step < 0
            or value.next_step > _MAX_STEPS
        ):
            _fail(f"{label} has an invalid step boundary")
        base = value.base_artifact
        if type(base) is not ArtifactIdentity:
            _fail(f"{label} has invalid base artifact evidence")
        canonical_base = ArtifactIdentity(
            artifact_ref=base.artifact_ref,
            sha256=base.sha256,
        )
        job_id = _require_text(value.job_id, name=f"{label} job_id")
        frozen_package_sha256 = _require_sha256(
            value.frozen_package_sha256,
            name=f"{label} frozen_package_sha256",
        )
        training_material_sha256 = _require_sha256(
            value.training_material_sha256,
            name=f"{label} training_material_sha256",
        )
        scale_authorization_sha256 = _require_sha256(
            value.scale_authorization_sha256,
            name=f"{label} scale_authorization_sha256",
        )
        execution_plan_sha256 = _require_sha256(
            value.execution_plan_sha256,
            name=f"{label} execution_plan_sha256",
        )
        job_fingerprint = _require_sha256(
            value.job_fingerprint,
            name=f"{label} job_fingerprint",
        )
        candidate_artifact_ref = _require_text(
            value.candidate_artifact_ref,
            name=f"{label} candidate_artifact_ref",
        )
        candidate_sha256 = value.candidate_sha256
        if candidate_sha256 is not None:
            candidate_sha256 = _require_sha256(
                candidate_sha256,
                name=f"{label} candidate_sha256",
            )
        checkpoint_id = value.checkpoint_id
        if checkpoint_id is not None:
            checkpoint_id = _require_text(
                checkpoint_id,
                name=f"{label} checkpoint_id",
            )
        reason = value.reason
        if reason is not None:
            reason = _require_text(reason, name=f"{label} reason")
    except (AttributeError, TypeError, ValueError) as exc:
        raise PhysicalTrainingPilotError(f"{label} evidence is not canonical") from exc
    return TrainingRunEvidence(
        job_id=job_id,
        state=state,
        next_step=value.next_step,
        base_artifact=canonical_base,
        frozen_package_sha256=frozen_package_sha256,
        training_material_sha256=training_material_sha256,
        scale_authorization_sha256=scale_authorization_sha256,
        execution_plan_sha256=execution_plan_sha256,
        job_fingerprint=job_fingerprint,
        candidate_artifact_ref=candidate_artifact_ref,
        candidate_sha256=candidate_sha256,
        checkpoint_id=checkpoint_id,
        reason=reason,
    )


def build_physical_training_pilot_report(
    *,
    paused: TrainingRunEvidence,
    restart_probe: TrainingRunEvidence,
    completed: TrainingRunEvidence,
    trainer_job_fingerprint: str,
    trainer_deployment_identity: ArtifactIdentity,
    candidate_path: Path,
    candidate_descriptor: ModelArtifactDescriptor,
    candidate_root: Path | None = None,
) -> PhysicalTrainingPilotReport:
    """Build path-free evidence from one durable pause/restart/completion sequence."""

    if not _is_windows():
        _fail("physical PEFT pilot report must be built on Windows")
    paused = _snapshot_run_evidence(
        paused,
        state=TrainingRunState.PAUSED,
        label="paused run",
    )
    restart_probe = _snapshot_run_evidence(
        restart_probe,
        state=TrainingRunState.PAUSED,
        label="restart probe",
    )
    completed = _snapshot_run_evidence(
        completed,
        state=TrainingRunState.COMPLETED,
        label="completed run",
    )
    if paused.next_step != 1:
        _fail("physical pilot must pause exactly after its first trainer step")
    if restart_probe.next_step != 1:
        _fail("restarted runtime did not reopen the one-step durable checkpoint")
    if completed.next_step < 2:
        _fail("physical pilot must complete after the restart boundary")
    if paused.candidate_sha256 is not None or restart_probe.candidate_sha256 is not None:
        _fail("paused pilot evidence must not already publish a candidate")
    checkpoint_ids = (
        paused.checkpoint_id,
        restart_probe.checkpoint_id,
        completed.checkpoint_id,
    )
    if any(checkpoint_id is None for checkpoint_id in checkpoint_ids):
        _fail("physical pilot requires pause, restart-probe, and completion checkpoints")
    if len(set(checkpoint_ids)) != 3:
        _fail("physical pilot did not advance all durable checkpoint identities")
    if paused.reason != "paused":
        _fail("physical pilot pause must come from the explicit pause control")
    if restart_probe.reason != "paused_before_admission":
        _fail("restart probe must pause before admission and trainer effects")

    identity_fields = (
        "job_id",
        "base_artifact",
        "frozen_package_sha256",
        "training_material_sha256",
        "scale_authorization_sha256",
        "execution_plan_sha256",
        "job_fingerprint",
        "candidate_artifact_ref",
    )
    for name in identity_fields:
        paused_value = getattr(paused, name)
        if getattr(restart_probe, name) != paused_value:
            _fail(f"restart probe changed {name} across reopen")
        if getattr(completed, name) != paused_value:
            _fail(f"physical pilot changed {name} across restart")
    if completed.candidate_sha256 is None:
        _fail("completed pilot is missing candidate digest evidence")

    stable_candidate_path = _require_path(candidate_path, name="candidate_path")
    stability_lock = _open_windows_candidate_stability_lock(stable_candidate_path)
    try:
        receipt = _verify_candidate_receipt(
            candidate_path=stable_candidate_path,
            candidate_descriptor=candidate_descriptor,
            candidate_root=candidate_root,
        )
        if receipt.sha256 != completed.candidate_sha256:
            _fail("physical candidate receipt does not match completed runtime evidence")
        manifest_evidence = _candidate_manifest_evidence(
            candidate_path=stable_candidate_path,
            completed=completed,
            trainer_job_fingerprint=trainer_job_fingerprint,
            trainer_deployment_identity=trainer_deployment_identity,
        )
    finally:
        _close_windows_candidate_stability_lock(stability_lock)

    return PhysicalTrainingPilotReport(
        job_id=completed.job_id,
        base_sha256=completed.base_artifact.sha256,
        frozen_package_sha256=completed.frozen_package_sha256,
        training_material_sha256=completed.training_material_sha256,
        scale_authorization_sha256=completed.scale_authorization_sha256,
        execution_plan_sha256=completed.execution_plan_sha256,
        job_fingerprint=completed.job_fingerprint,
        trainer_job_fingerprint=manifest_evidence.trainer_job_fingerprint,
        paused_checkpoint_id=paused.checkpoint_id,
        restart_checkpoint_id=restart_probe.checkpoint_id,
        completed_checkpoint_id=completed.checkpoint_id,
        candidate_artifact_ref=completed.candidate_artifact_ref,
        candidate_descriptor_sha256=receipt.descriptor_digest,
        candidate_registry_key=receipt.registry_key,
        candidate_sha256=receipt.sha256,
        candidate_byte_count=receipt.size_bytes,
        candidate_manifest_sha256=manifest_evidence.candidate_manifest_sha256,
        consumed_materials_sha256=manifest_evidence.consumed_materials_sha256,
        model_dir_manifest_sha256=manifest_evidence.model_dir_manifest_sha256,
        trainer_artifact_id=manifest_evidence.trainer_artifact_id,
        trainer_deployment_sha256=manifest_evidence.trainer_deployment_sha256,
        trainer_implementation_sha256=manifest_evidence.trainer_implementation_sha256,
        training_runtime_manifest_sha256=(
            manifest_evidence.training_runtime_manifest_sha256
        ),
        completed_steps=completed.next_step,
    )


def _snapshot_job_spec(spec: object) -> TrainingJobSpec:
    if type(spec) is not TrainingJobSpec:
        raise TypeError("spec must be an exact TrainingJobSpec")
    try:
        base = spec.base_artifact
        if type(base) is not ArtifactIdentity:
            raise TypeError("base_artifact must be exact ArtifactIdentity")
        return TrainingJobSpec(
            job_id=spec.job_id,
            task_id=spec.task_id,
            project_id=spec.project_id,
            owner_id=spec.owner_id,
            base_artifact=ArtifactIdentity(
                artifact_ref=base.artifact_ref,
                sha256=base.sha256,
            ),
            frozen_package_sha256=spec.frozen_package_sha256,
            training_material_sha256=spec.training_material_sha256,
            scale_authorization_sha256=spec.scale_authorization_sha256,
            candidate_artifact_ref=spec.candidate_artifact_ref,
            max_steps=spec.max_steps,
            resource_scope=spec.resource_scope,
        )
    except (AttributeError, TypeError, ValueError) as exc:
        raise PhysicalTrainingPilotError(
            "physical pilot job specification is not canonical"
        ) from exc


def _resolve_candidate_descriptor(
    factory: Callable[[TrainingRunEvidence], ModelArtifactDescriptor],
    completed: TrainingRunEvidence,
) -> ModelArtifactDescriptor:
    if not callable(factory):
        raise TypeError("candidate_descriptor_factory must be callable")
    callback_evidence = _snapshot_run_evidence(
        completed,
        state=TrainingRunState.COMPLETED,
        label="candidate descriptor context",
    )
    descriptor = factory(callback_evidence)
    if type(descriptor) is not ModelArtifactDescriptor:
        raise TypeError(
            "candidate_descriptor_factory must return exact ModelArtifactDescriptor"
        )
    return descriptor


def run_physical_training_pilot(
    *,
    runtime: TrainingRuntime,
    restart_runtime: Callable[[], TrainingRuntime],
    spec: TrainingJobSpec,
    worker: SubprocessTrainingWorker,
    restart_worker: Callable[[], SubprocessTrainingWorker],
    scale_authorization: TrainingScaleAuthorization,
    candidate_path: Path,
    candidate_descriptor_factory: Callable[
        [TrainingRunEvidence],
        ModelArtifactDescriptor,
    ],
    candidate_root: Path | None = None,
) -> PhysicalTrainingPilotReport:
    """Exercise one real Windows subprocess step, reopen, resume, and verify candidate bytes."""

    if not _is_windows():
        _fail("physical PEFT pilot must execute on Windows")
    if type(runtime) is not TrainingRuntime:
        raise TypeError("runtime must be the canonical TrainingRuntime")
    canonical_spec = _snapshot_job_spec(spec)
    if type(worker) is not SubprocessTrainingWorker:
        raise TypeError("worker must be the canonical SubprocessTrainingWorker")
    if type(scale_authorization) is not TrainingScaleAuthorization:
        raise TypeError("scale_authorization must be exact TrainingScaleAuthorization")
    if not callable(restart_runtime) or not callable(restart_worker):
        raise TypeError("restart factories must be callable")
    if not callable(candidate_descriptor_factory):
        raise TypeError("candidate_descriptor_factory must be callable")
    if canonical_spec.max_steps < 2:
        _fail("physical pilot requires max_steps >= 2")
    initial_execution_plan_sha256 = _require_sha256(
        worker.execution_plan_sha256,
        name="initial worker execution_plan_sha256",
    )
    initial_trainer_job_fingerprint = _require_sha256(
        worker.protocol_job_fingerprint(canonical_spec),
        name="initial worker trainer_job_fingerprint",
    )
    initial_trainer_deployment_identity = (
        worker.verified_trainer_deployment_identity()
    )
    if type(initial_trainer_deployment_identity) is not ArtifactIdentity:
        raise TypeError("worker trainer deployment identity must be exact ArtifactIdentity")

    control_reads = 0

    def one_step_then_pause() -> TrainingControl:
        nonlocal control_reads
        control_reads += 1
        if control_reads <= 2:
            return TrainingControl.CONTINUE
        return TrainingControl.PAUSE

    paused = runtime.run(
        canonical_spec,
        worker,
        scale_authorization=scale_authorization,
        control=one_step_then_pause,
    )
    paused = _snapshot_run_evidence(
        paused,
        state=TrainingRunState.PAUSED,
        label="paused run",
    )
    if paused.next_step != 1:
        _fail("trainer did not reach the required one-step durable pause boundary")
    if paused.reason != "paused":
        _fail("initial pilot pause did not come from the explicit pause control")

    resumed_runtime = restart_runtime()
    resumed_worker = restart_worker()
    if type(resumed_runtime) is not TrainingRuntime:
        raise TypeError("restart_runtime must return canonical TrainingRuntime")
    if type(resumed_worker) is not SubprocessTrainingWorker:
        raise TypeError("restart_worker must return canonical SubprocessTrainingWorker")
    if resumed_runtime is runtime or resumed_worker is worker:
        _fail("restart factories must construct new runtime and worker objects")
    resumed_execution_plan_sha256 = _require_sha256(
        resumed_worker.execution_plan_sha256,
        name="resumed worker execution_plan_sha256",
    )
    if not hmac.compare_digest(
        resumed_execution_plan_sha256,
        initial_execution_plan_sha256,
    ):
        _fail("trainer execution plan changed across restart")
    resumed_trainer_job_fingerprint = _require_sha256(
        resumed_worker.protocol_job_fingerprint(canonical_spec),
        name="resumed worker trainer_job_fingerprint",
    )
    if not hmac.compare_digest(
        resumed_trainer_job_fingerprint,
        initial_trainer_job_fingerprint,
    ):
        _fail("trainer protocol job identity changed across restart")
    resumed_trainer_deployment_identity = (
        resumed_worker.verified_trainer_deployment_identity()
    )
    if type(resumed_trainer_deployment_identity) is not ArtifactIdentity:
        raise TypeError("worker trainer deployment identity must be exact ArtifactIdentity")
    if resumed_trainer_deployment_identity != initial_trainer_deployment_identity:
        _fail("verified trainer deployment identity changed across restart")

    restart_probe = resumed_runtime.run(
        canonical_spec,
        resumed_worker,
        scale_authorization=scale_authorization,
        control=lambda: TrainingControl.PAUSE,
    )
    restart_probe = _snapshot_run_evidence(
        restart_probe,
        state=TrainingRunState.PAUSED,
        label="restart probe",
    )
    if restart_probe.next_step != 1:
        _fail("restarted runtime did not reopen the one-step durable checkpoint")
    if restart_probe.reason != "paused_before_admission":
        _fail("restart probe did not pause before admission and trainer effects")

    completed = resumed_runtime.run(
        canonical_spec,
        resumed_worker,
        scale_authorization=scale_authorization,
    )
    completed = _snapshot_run_evidence(
        completed,
        state=TrainingRunState.COMPLETED,
        label="completed run",
    )
    candidate_descriptor = _resolve_candidate_descriptor(
        candidate_descriptor_factory,
        completed,
    )
    return build_physical_training_pilot_report(
        paused=paused,
        restart_probe=restart_probe,
        completed=completed,
        trainer_job_fingerprint=resumed_trainer_job_fingerprint,
        trainer_deployment_identity=resumed_trainer_deployment_identity,
        candidate_path=candidate_path,
        candidate_descriptor=candidate_descriptor,
        candidate_root=candidate_root,
    )
