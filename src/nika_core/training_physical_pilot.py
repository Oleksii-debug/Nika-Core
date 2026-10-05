from __future__ import annotations

import hashlib
import hmac
import json
import os
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

_SCHEMA_VERSION = 3
_REPORT_DOMAIN = b"nika-peft-physical-pilot-report-v3\x00"
_MAX_REPORT_BYTES = 32 * 1024
_MAX_TEXT_BYTES = 1024
_MAX_STEPS = 1_000_000
_PLATFORM_PATH_TYPE = type(Path())
_REQUIRED_REPORT_FIELDS = {
    "base_sha256",
    "candidate_artifact_ref",
    "candidate_byte_count",
    "candidate_descriptor_sha256",
    "candidate_registry_key",
    "candidate_sha256",
    "completed_checkpoint_id",
    "completed_steps",
    "consumed_materials_sha256",
    "execution_plan_sha256",
    "frozen_package_sha256",
    "job_fingerprint",
    "job_id",
    "model_dir_manifest_sha256",
    "paused_checkpoint_id",
    "previous_adapter_sha256",
    "restart_checkpoint_id",
    "platform",
    "scale_authorization_sha256",
    "schema_version",
    "trained_adapter_sha256",
    "trainer_artifact_id",
    "trainer_implementation_sha256",
    "trainer_sha256",
    "training_material_sha256",
    "training_runtime_manifest_sha256",
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
class PhysicalTrainingPilotReport:
    """Minimized, path-free evidence for one checkpoint/restart PEFT pilot."""

    job_id: str
    base_sha256: str
    frozen_package_sha256: str
    training_material_sha256: str
    scale_authorization_sha256: str
    execution_plan_sha256: str
    job_fingerprint: str
    consumed_materials_sha256: str
    model_dir_manifest_sha256: str
    previous_adapter_sha256: str
    trained_adapter_sha256: str
    trainer_artifact_id: str
    trainer_implementation_sha256: str
    trainer_sha256: str
    training_runtime_manifest_sha256: str
    paused_checkpoint_id: str
    restart_checkpoint_id: str
    completed_checkpoint_id: str
    candidate_artifact_ref: str
    candidate_descriptor_sha256: str
    candidate_registry_key: str
    candidate_sha256: str
    candidate_byte_count: int
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
            (self.consumed_materials_sha256, "consumed_materials_sha256"),
            (self.model_dir_manifest_sha256, "model_dir_manifest_sha256"),
            (self.previous_adapter_sha256, "previous_adapter_sha256"),
            (self.trained_adapter_sha256, "trained_adapter_sha256"),
            (self.trainer_artifact_id, "trainer_artifact_id"),
            (self.trainer_implementation_sha256, "trainer_implementation_sha256"),
            (self.trainer_sha256, "trainer_sha256"),
            (self.training_runtime_manifest_sha256, "training_runtime_manifest_sha256"),
            (self.candidate_descriptor_sha256, "candidate_descriptor_sha256"),
            (self.candidate_registry_key, "candidate_registry_key"),
            (self.candidate_sha256, "candidate_sha256"),
        ):
            _require_sha256(value, name=name)
        if hmac.compare_digest(
            self.previous_adapter_sha256,
            self.trained_adapter_sha256,
        ):
            _fail("physical pilot did not prove trainable adapter weight mutation")
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
        if type(self.completed_steps) is not int or self.completed_steps != 2:
            _fail("physical pilot must complete exactly two bounded trainer steps")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "base_sha256": self.base_sha256,
            "candidate_artifact_ref": self.candidate_artifact_ref,
            "candidate_byte_count": self.candidate_byte_count,
            "candidate_descriptor_sha256": self.candidate_descriptor_sha256,
            "candidate_registry_key": self.candidate_registry_key,
            "candidate_sha256": self.candidate_sha256,
            "completed_checkpoint_id": self.completed_checkpoint_id,
            "completed_steps": self.completed_steps,
            "consumed_materials_sha256": self.consumed_materials_sha256,
            "execution_plan_sha256": self.execution_plan_sha256,
            "frozen_package_sha256": self.frozen_package_sha256,
            "job_fingerprint": self.job_fingerprint,
            "job_id": self.job_id,
            "model_dir_manifest_sha256": self.model_dir_manifest_sha256,
            "paused_checkpoint_id": self.paused_checkpoint_id,
            "previous_adapter_sha256": self.previous_adapter_sha256,
            "restart_checkpoint_id": self.restart_checkpoint_id,
            "platform": self.platform,
            "scale_authorization_sha256": self.scale_authorization_sha256,
            "schema_version": self.schema_version,
            "trained_adapter_sha256": self.trained_adapter_sha256,
            "trainer_artifact_id": self.trainer_artifact_id,
            "trainer_implementation_sha256": self.trainer_implementation_sha256,
            "trainer_sha256": self.trainer_sha256,
            "training_material_sha256": self.training_material_sha256,
            "training_runtime_manifest_sha256": self.training_runtime_manifest_sha256,
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
            consumed_materials_sha256=value["consumed_materials_sha256"],
            model_dir_manifest_sha256=value["model_dir_manifest_sha256"],
            previous_adapter_sha256=value["previous_adapter_sha256"],
            trained_adapter_sha256=value["trained_adapter_sha256"],
            trainer_artifact_id=value["trainer_artifact_id"],
            trainer_implementation_sha256=value["trainer_implementation_sha256"],
            trainer_sha256=value["trainer_sha256"],
            training_runtime_manifest_sha256=value["training_runtime_manifest_sha256"],
            paused_checkpoint_id=value["paused_checkpoint_id"],
            restart_checkpoint_id=value["restart_checkpoint_id"],
            completed_checkpoint_id=value["completed_checkpoint_id"],
            candidate_artifact_ref=value["candidate_artifact_ref"],
            candidate_descriptor_sha256=value["candidate_descriptor_sha256"],
            candidate_registry_key=value["candidate_registry_key"],
            candidate_sha256=value["candidate_sha256"],
            candidate_byte_count=value["candidate_byte_count"],
            completed_steps=value["completed_steps"],
            platform=value["platform"],
            schema_version=value["schema_version"],
        )


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


def _peft_candidate_manifest_evidence(
    *,
    candidate_path: Path,
    completed: TrainingRunEvidence,
) -> dict[str, str]:
    path = _require_path(candidate_path, name="candidate_path")
    try:
        manifest = candidate_adapter_manifest(path)
    except (PeftTrainerError, TypeError, ValueError) as exc:
        raise PhysicalTrainingPilotError(
            "canonical PEFT candidate manifest verification failed"
        ) from exc
    if type(manifest) is not dict:
        _fail("canonical PEFT candidate manifest returned invalid evidence")
    if (
        manifest.get("base_artifact_ref") != completed.base_artifact.artifact_ref
        or manifest.get("base_artifact_sha256") != completed.base_artifact.sha256
        or manifest.get("candidate_artifact_ref") != completed.candidate_artifact_ref
        or manifest.get("job_fingerprint") != completed.job_fingerprint
        or manifest.get("step_number") != completed.next_step
    ):
        _fail("PEFT candidate manifest does not match completed runtime identity")
    names = (
        "consumed_materials_sha256",
        "model_dir_manifest_sha256",
        "previous_adapter_sha256",
        "trained_adapter_sha256",
        "trainer_artifact_id",
        "trainer_implementation_sha256",
        "trainer_sha256",
        "training_runtime_manifest_sha256",
    )
    evidence = {
        name: _require_sha256(manifest.get(name), name=name)
        for name in names
    }
    if hmac.compare_digest(
        evidence["previous_adapter_sha256"],
        evidence["trained_adapter_sha256"],
    ):
        _fail("PEFT candidate manifest does not prove adapter weight mutation")
    return evidence


def build_physical_training_pilot_report(
    *,
    paused: TrainingRunEvidence,
    restart_probe: TrainingRunEvidence,
    completed: TrainingRunEvidence,
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
    if completed.next_step != 2:
        _fail("physical pilot must complete exactly two bounded trainer steps")
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

    receipt = _verify_candidate_receipt(
        candidate_path=candidate_path,
        candidate_descriptor=candidate_descriptor,
        candidate_root=candidate_root,
    )
    if receipt.sha256 != completed.candidate_sha256:
        _fail("physical candidate receipt does not match completed runtime evidence")
    manifest_evidence = _peft_candidate_manifest_evidence(
        candidate_path=candidate_path,
        completed=completed,
    )

    return PhysicalTrainingPilotReport(
        job_id=completed.job_id,
        base_sha256=completed.base_artifact.sha256,
        frozen_package_sha256=completed.frozen_package_sha256,
        training_material_sha256=completed.training_material_sha256,
        scale_authorization_sha256=completed.scale_authorization_sha256,
        execution_plan_sha256=completed.execution_plan_sha256,
        job_fingerprint=completed.job_fingerprint,
        consumed_materials_sha256=manifest_evidence["consumed_materials_sha256"],
        model_dir_manifest_sha256=manifest_evidence["model_dir_manifest_sha256"],
        previous_adapter_sha256=manifest_evidence["previous_adapter_sha256"],
        trained_adapter_sha256=manifest_evidence["trained_adapter_sha256"],
        trainer_artifact_id=manifest_evidence["trainer_artifact_id"],
        trainer_implementation_sha256=manifest_evidence[
            "trainer_implementation_sha256"
        ],
        trainer_sha256=manifest_evidence["trainer_sha256"],
        training_runtime_manifest_sha256=manifest_evidence[
            "training_runtime_manifest_sha256"
        ],
        paused_checkpoint_id=paused.checkpoint_id,
        restart_checkpoint_id=restart_probe.checkpoint_id,
        completed_checkpoint_id=completed.checkpoint_id,
        candidate_artifact_ref=completed.candidate_artifact_ref,
        candidate_descriptor_sha256=receipt.descriptor_digest,
        candidate_registry_key=receipt.registry_key,
        candidate_sha256=receipt.sha256,
        candidate_byte_count=receipt.size_bytes,
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
    if canonical_spec.max_steps != 2:
        _fail("physical pilot requires exactly max_steps == 2")
    initial_execution_plan_sha256 = _require_sha256(
        worker.execution_plan_sha256,
        name="initial worker execution_plan_sha256",
    )

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
        candidate_path=candidate_path,
        candidate_descriptor=candidate_descriptor,
        candidate_root=candidate_root,
    )
