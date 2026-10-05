from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn

from nika_core.training_adapters import SubprocessTrainingWorker
from nika_core.training_runtime import (
    TrainingControl,
    TrainingJobSpec,
    TrainingRunEvidence,
    TrainingRunState,
    TrainingRuntime,
)
from nika_core.training_scale import TrainingScaleAuthorization

_SCHEMA_VERSION = 1
_REPORT_DOMAIN = b"nika-peft-physical-pilot-report-v1\x00"
_MAX_REPORT_BYTES = 32 * 1024
_MAX_TEXT_BYTES = 1024
_READ_CHUNK_BYTES = 1024 * 1024
_PLATFORM_PATH_TYPE = type(Path())
_REQUIRED_REPORT_FIELDS = {
    "base_sha256",
    "candidate_artifact_ref",
    "candidate_byte_count",
    "candidate_sha256",
    "completed_checkpoint_id",
    "completed_steps",
    "execution_plan_sha256",
    "frozen_package_sha256",
    "job_fingerprint",
    "job_id",
    "paused_checkpoint_id",
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


def _is_reparse_point(value: os.stat_result) -> bool:
    attributes = int(getattr(value, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & reparse_flag)


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _verify_candidate_file(candidate_path: Path) -> tuple[str, int]:
    if type(candidate_path) is not _PLATFORM_PATH_TYPE or not candidate_path.is_absolute():
        _fail("candidate path must be an absolute canonical platform Path")
    try:
        before = os.lstat(candidate_path)
    except OSError as exc:
        raise PhysicalTrainingPilotError("candidate file is not accessible") from exc
    if stat.S_ISLNK(before.st_mode) or _is_reparse_point(before):
        _fail("candidate file must not be a symbolic link or reparse point")
    if not stat.S_ISREG(before.st_mode):
        _fail("candidate path must identify a regular file")
    if before.st_size <= 0:
        _fail("candidate file must not be empty")
    before_identity = _stat_identity(before)

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate_path, flags)
    except OSError as exc:
        raise PhysicalTrainingPilotError("candidate file could not be opened safely") from exc

    digest = hashlib.sha256()
    byte_count = 0
    try:
        try:
            opened = os.fstat(descriptor)
        except OSError as exc:
            raise PhysicalTrainingPilotError("candidate metadata could not be read") from exc
        if not stat.S_ISREG(opened.st_mode):
            _fail("opened candidate must remain a regular file")
        opened_identity = _stat_identity(opened)
        if os.name != "nt" and opened_identity != before_identity:
            _fail("candidate changed before verification")
        while True:
            try:
                chunk = os.read(descriptor, _READ_CHUNK_BYTES)
            except OSError as exc:
                raise PhysicalTrainingPilotError("candidate bytes could not be read") from exc
            if not chunk:
                break
            byte_count += len(chunk)
            digest.update(chunk)
        try:
            after_open = os.fstat(descriptor)
        except OSError as exc:
            raise PhysicalTrainingPilotError("candidate metadata could not be re-read") from exc
        if _stat_identity(after_open) != opened_identity:
            _fail("candidate changed during verification")
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass

    try:
        after_path = os.lstat(candidate_path)
    except OSError as exc:
        raise PhysicalTrainingPilotError("candidate path disappeared after verification") from exc
    if stat.S_ISLNK(after_path.st_mode) or _is_reparse_point(after_path):
        _fail("candidate path became a symbolic link or reparse point")
    if not stat.S_ISREG(after_path.st_mode):
        _fail("candidate path stopped identifying a regular file")
    if os.name != "nt" and _stat_identity(after_path) != before_identity:
        _fail("candidate path changed during verification")
    if byte_count != opened.st_size or byte_count != after_path.st_size:
        _fail("candidate size changed during verification")
    return digest.hexdigest(), byte_count


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
    paused_checkpoint_id: str
    completed_checkpoint_id: str
    candidate_artifact_ref: str
    candidate_sha256: str
    candidate_byte_count: int
    completed_steps: int
    platform: str = "windows"
    schema_version: int = _SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != _SCHEMA_VERSION:
            _fail("unsupported physical pilot report schema")
        if self.platform != "windows" or type(self.platform) is not str:
            _fail("physical PEFT pilot report must identify Windows")
        for value, name in (
            (self.job_id, "job_id"),
            (self.paused_checkpoint_id, "paused_checkpoint_id"),
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
            (self.candidate_sha256, "candidate_sha256"),
        ):
            _require_sha256(value, name=name)
        if self.paused_checkpoint_id == self.completed_checkpoint_id:
            _fail("pause and completion must have distinct durable checkpoints")
        if (
            type(self.candidate_byte_count) is not int
            or self.candidate_byte_count <= 0
            or self.candidate_byte_count > (1 << 63) - 1
        ):
            _fail("candidate_byte_count must be a positive signed-64 integer")
        if (
            type(self.completed_steps) is not int
            or self.completed_steps < 2
            or self.completed_steps > 1_000_000
        ):
            _fail("physical pilot must complete after a real restart boundary")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "base_sha256": self.base_sha256,
            "candidate_artifact_ref": self.candidate_artifact_ref,
            "candidate_byte_count": self.candidate_byte_count,
            "candidate_sha256": self.candidate_sha256,
            "completed_checkpoint_id": self.completed_checkpoint_id,
            "completed_steps": self.completed_steps,
            "execution_plan_sha256": self.execution_plan_sha256,
            "frozen_package_sha256": self.frozen_package_sha256,
            "job_fingerprint": self.job_fingerprint,
            "job_id": self.job_id,
            "paused_checkpoint_id": self.paused_checkpoint_id,
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
        try:
            return cls(
                job_id=value["job_id"],
                base_sha256=value["base_sha256"],
                frozen_package_sha256=value["frozen_package_sha256"],
                training_material_sha256=value["training_material_sha256"],
                scale_authorization_sha256=value["scale_authorization_sha256"],
                execution_plan_sha256=value["execution_plan_sha256"],
                job_fingerprint=value["job_fingerprint"],
                paused_checkpoint_id=value["paused_checkpoint_id"],
                completed_checkpoint_id=value["completed_checkpoint_id"],
                candidate_artifact_ref=value["candidate_artifact_ref"],
                candidate_sha256=value["candidate_sha256"],
                candidate_byte_count=value["candidate_byte_count"],
                completed_steps=value["completed_steps"],
                platform=value["platform"],
                schema_version=value["schema_version"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PhysicalTrainingPilotError("pilot report fields are invalid") from exc


def _require_run_evidence(
    value: object,
    *,
    state: TrainingRunState,
    label: str,
) -> TrainingRunEvidence:
    if type(value) is not TrainingRunEvidence:
        _fail(f"{label} must be exact TrainingRunEvidence")
    if value.state is not state:
        _fail(f"{label} has an unexpected training state")
    return value


def build_physical_training_pilot_report(
    *,
    paused: TrainingRunEvidence,
    completed: TrainingRunEvidence,
    candidate_path: Path,
) -> PhysicalTrainingPilotReport:
    """Build a path-free report from one durable pause/restart/completion sequence."""

    paused = _require_run_evidence(
        paused,
        state=TrainingRunState.PAUSED,
        label="paused run",
    )
    completed = _require_run_evidence(
        completed,
        state=TrainingRunState.COMPLETED,
        label="completed run",
    )
    if paused.next_step != 1:
        _fail("physical pilot must pause exactly after its first trainer step")
    if completed.next_step < 2:
        _fail("physical pilot must complete after the restart boundary")
    if paused.candidate_sha256 is not None:
        _fail("paused pilot must not already publish candidate evidence")
    if paused.checkpoint_id is None or completed.checkpoint_id is None:
        _fail("physical pilot requires durable pause and completion checkpoints")
    if paused.checkpoint_id == completed.checkpoint_id:
        _fail("physical pilot did not advance durable checkpoint identity")

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
        if getattr(paused, name) != getattr(completed, name):
            _fail(f"physical pilot changed {name} across restart")
    if completed.candidate_sha256 is None:
        _fail("completed pilot is missing candidate digest evidence")

    candidate_sha256, candidate_byte_count = _verify_candidate_file(candidate_path)
    if candidate_sha256 != completed.candidate_sha256:
        _fail("physical candidate bytes do not match completed runtime evidence")

    return PhysicalTrainingPilotReport(
        job_id=completed.job_id,
        base_sha256=completed.base_artifact.sha256,
        frozen_package_sha256=completed.frozen_package_sha256,
        training_material_sha256=completed.training_material_sha256,
        scale_authorization_sha256=completed.scale_authorization_sha256,
        execution_plan_sha256=completed.execution_plan_sha256,
        job_fingerprint=completed.job_fingerprint,
        paused_checkpoint_id=paused.checkpoint_id,
        completed_checkpoint_id=completed.checkpoint_id,
        candidate_artifact_ref=completed.candidate_artifact_ref,
        candidate_sha256=completed.candidate_sha256,
        candidate_byte_count=candidate_byte_count,
        completed_steps=completed.next_step,
    )


def run_physical_training_pilot(
    *,
    runtime: TrainingRuntime,
    restart_runtime: Callable[[], TrainingRuntime],
    spec: TrainingJobSpec,
    worker: SubprocessTrainingWorker,
    restart_worker: Callable[[], SubprocessTrainingWorker],
    scale_authorization: TrainingScaleAuthorization,
    candidate_path: Path,
) -> PhysicalTrainingPilotReport:
    """Exercise one real Windows subprocess step, reopen, resume, and verify candidate bytes."""

    if not _is_windows():
        _fail("physical PEFT pilot must execute on Windows")
    if type(runtime) is not TrainingRuntime:
        raise TypeError("runtime must be the canonical TrainingRuntime")
    if type(spec) is not TrainingJobSpec:
        raise TypeError("spec must be an exact TrainingJobSpec")
    if type(worker) is not SubprocessTrainingWorker:
        raise TypeError("worker must be the canonical SubprocessTrainingWorker")
    if type(scale_authorization) is not TrainingScaleAuthorization:
        raise TypeError("scale_authorization must be exact TrainingScaleAuthorization")
    if not callable(restart_runtime) or not callable(restart_worker):
        raise TypeError("restart factories must be callable")
    if spec.max_steps < 2:
        _fail("physical pilot requires max_steps >= 2")

    control_reads = 0

    def one_step_then_pause() -> TrainingControl:
        nonlocal control_reads
        control_reads += 1
        if control_reads <= 2:
            return TrainingControl.CONTINUE
        return TrainingControl.PAUSE

    paused = runtime.run(
        spec,
        worker,
        scale_authorization=scale_authorization,
        control=one_step_then_pause,
    )
    paused = _require_run_evidence(
        paused,
        state=TrainingRunState.PAUSED,
        label="paused run",
    )
    if paused.next_step != 1:
        _fail("trainer did not reach the required one-step durable pause boundary")

    resumed_runtime = restart_runtime()
    resumed_worker = restart_worker()
    if type(resumed_runtime) is not TrainingRuntime:
        raise TypeError("restart_runtime must return canonical TrainingRuntime")
    if type(resumed_worker) is not SubprocessTrainingWorker:
        raise TypeError("restart_worker must return canonical SubprocessTrainingWorker")
    if resumed_runtime is runtime or resumed_worker is worker:
        _fail("restart factories must construct new runtime and worker objects")
    if resumed_worker.execution_plan_sha256 != worker.execution_plan_sha256:
        _fail("trainer execution plan changed across restart")

    completed = resumed_runtime.run(
        spec,
        resumed_worker,
        scale_authorization=scale_authorization,
    )
    return build_physical_training_pilot_report(
        paused=paused,
        completed=completed,
        candidate_path=candidate_path,
    )
