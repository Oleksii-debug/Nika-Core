from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Callable
from typing import TYPE_CHECKING

from nika_core.kernel.checkpoint import Checkpoint, CheckpointService
from nika_core.resources.manager import ResourceManager
from nika_core.training_materials import ResolvedTrainingPackage, TrainingMaterialResolutionError
from nika_core.training_runtime.contracts import (
    TrainingControl,
    TrainingJobSpec,
    TrainingRunEvidence,
    TrainingRunState,
    TrainingStepResult,
    TrainingWorkerError,
    TrainingWorkerFailureEffect,
    TrainingWorkerPort,
)

if TYPE_CHECKING:
    from nika_core.training_scale import TrainingScaleAuthorization

_CHECKPOINT_PREFIX = "training_runtime/v4/"
_LEGACY_CHECKPOINT_PREFIXES = ("training_runtime/v3/",)
_CHECKPOINT_SCHEMA_VERSION = 4
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_TERMINAL_STATES = {
    TrainingRunState.COMPLETED,
    TrainingRunState.CANCELLED,
    TrainingRunState.RECONCILE_REQUIRED,
    TrainingRunState.EXHAUSTED,
}


class TrainingCheckpointError(RuntimeError):
    """Raised when durable training state cannot be trusted for safe resume."""


def _require_execution_plan_sha256(value: object) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(
            "training execution plan must be an exact lowercase SHA-256 digest"
        )
    return value


def training_job_fingerprint(
    spec: TrainingJobSpec,
    *,
    execution_plan_sha256: str,
) -> str:
    """Bind one durable training job to the exact result-affecting trainer plan."""

    if type(spec) is not TrainingJobSpec:
        raise TypeError("spec must be an exact TrainingJobSpec")
    plan_sha256 = _require_execution_plan_sha256(execution_plan_sha256)
    identity = {
        "job_id": spec.job_id,
        "task_id": spec.task_id,
        "project_id": spec.project_id,
        "owner_id": spec.owner_id,
        "base_artifact_ref": spec.base_artifact.artifact_ref,
        "base_sha256": spec.base_artifact.sha256,
        "frozen_package_sha256": spec.frozen_package_sha256,
        "training_material_sha256": spec.training_material_sha256,
        "scale_authorization_sha256": spec.scale_authorization_sha256,
        "execution_plan_sha256": plan_sha256,
        "candidate_artifact_ref": spec.candidate_artifact_ref,
        "max_steps": spec.max_steps,
        "resource_scope": spec.resource_scope,
    }
    body = json.dumps(identity, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(b"nika-training-runtime-job-v5\x00" + body.encode()).hexdigest()


def _worker_execution_plan_sha256(worker: TrainingWorkerPort) -> str:
    try:
        value = worker.execution_plan_sha256
    except AttributeError as exc:
        raise TypeError("training worker must expose execution_plan_sha256") from exc
    return _require_execution_plan_sha256(value)


def _stage(state: TrainingRunState) -> str:
    return f"{_CHECKPOINT_PREFIX}{state.value}"


def _read_control(control: Callable[[], TrainingControl]) -> TrainingControl:
    value = control()
    if type(value) is not TrainingControl:
        raise TypeError("training control callback must return an exact TrainingControl")
    return value


def _material_identity_matches(
    spec: TrainingJobSpec,
    materials: ResolvedTrainingPackage,
) -> bool:
    try:
        return (
            hmac.compare_digest(
                materials.training_material_sha256,
                spec.training_material_sha256,
            )
            and hmac.compare_digest(
                materials.evidence.package_manifest_sha256,
                spec.frozen_package_sha256,
            )
            and hmac.compare_digest(
                materials.evidence.base_artifact_sha256,
                spec.base_artifact.sha256,
            )
        )
    except (AttributeError, TypeError, ValueError):
        return False


class TrainingRuntime:
    """Executes bounded model-training work without owning scheduling or promotion policy.

    The supplied task must be dedicated to this training job. Durable checkpoints use the
    canonical task checkpoint store; ResourceManager remains the sole admission authority.
    Before every worker effect a DISPATCHING checkpoint is committed. A process crash or
    untyped worker exception therefore leaves durable uncertainty and can never blindly replay.
    """

    def __init__(
        self,
        *,
        resources: ResourceManager,
        checkpoints: CheckpointService,
        training_materials: ResolvedTrainingPackage,
    ) -> None:
        if type(training_materials) is not ResolvedTrainingPackage:
            raise TypeError("training_materials must be an exact ResolvedTrainingPackage")
        self._resources = resources
        self._checkpoints = checkpoints
        self._training_materials = training_materials

    def run(
        self,
        spec: TrainingJobSpec,
        worker: TrainingWorkerPort,
        *,
        scale_authorization: TrainingScaleAuthorization,
        control: Callable[[], TrainingControl] | None = None,
    ) -> TrainingRunEvidence:
        control = control or (lambda: TrainingControl.CONTINUE)
        execution_plan_sha256 = _worker_execution_plan_sha256(worker)
        from nika_core.training_scale import TrainingScaleAuthorization

        if type(scale_authorization) is not TrainingScaleAuthorization:
            raise TypeError(
                "scale_authorization must be an exact TrainingScaleAuthorization"
            )
        scale_authorization.verify_for_runtime(
            spec=spec,
            material_evidence=self._training_materials.evidence,
            execution_plan_sha256=execution_plan_sha256,
        )
        fingerprint = training_job_fingerprint(
            spec,
            execution_plan_sha256=execution_plan_sha256,
        )
        checkpoint = self._checkpoints.latest(spec.task_id)
        state, next_step, resume_state, candidate_sha256, reason = self._restore(
            spec=spec,
            fingerprint=fingerprint,
            checkpoint=checkpoint,
        )

        if state is TrainingRunState.DISPATCHING:
            saved = self._save(
                spec,
                fingerprint=fingerprint,
                state=TrainingRunState.RECONCILE_REQUIRED,
                next_step=next_step,
                resume_state=resume_state,
                candidate_sha256=None,
                reason="previous_worker_effect_unknown",
            )
            return self._evidence(
                spec,
                execution_plan_sha256=execution_plan_sha256,
                job_fingerprint=fingerprint,
                state=TrainingRunState.RECONCILE_REQUIRED,
                next_step=next_step,
                candidate_sha256=None,
                checkpoint=saved,
                reason="previous_worker_effect_unknown",
            )

        if state in _TERMINAL_STATES:
            return self._evidence(
                spec,
                execution_plan_sha256=execution_plan_sha256,
                job_fingerprint=fingerprint,
                state=state,
                next_step=next_step,
                candidate_sha256=candidate_sha256,
                checkpoint=checkpoint,
                reason=reason,
            )

        requested_control = _read_control(control)
        if requested_control is TrainingControl.CANCEL:
            saved = self._save(
                spec,
                fingerprint=fingerprint,
                state=TrainingRunState.CANCELLED,
                next_step=next_step,
                resume_state=resume_state,
                candidate_sha256=candidate_sha256,
                reason="cancelled_before_admission",
            )
            return self._evidence(
                spec,
                execution_plan_sha256=execution_plan_sha256,
                job_fingerprint=fingerprint,
                state=TrainingRunState.CANCELLED,
                next_step=next_step,
                candidate_sha256=candidate_sha256,
                checkpoint=saved,
                reason="cancelled_before_admission",
            )
        if requested_control is TrainingControl.PAUSE:
            saved = self._save(
                spec,
                fingerprint=fingerprint,
                state=TrainingRunState.PAUSED,
                next_step=next_step,
                resume_state=resume_state,
                candidate_sha256=candidate_sha256,
                reason="paused_before_admission",
            )
            return self._evidence(
                spec,
                execution_plan_sha256=execution_plan_sha256,
                job_fingerprint=fingerprint,
                state=TrainingRunState.PAUSED,
                next_step=next_step,
                candidate_sha256=candidate_sha256,
                checkpoint=saved,
                reason="paused_before_admission",
            )

        decision = self._resources.request(
            scope=spec.resource_scope,
            owner_id=spec.owner_id,
            request_id=spec.job_id,
        )
        if not decision.granted:
            return TrainingRunEvidence(
                job_id=spec.job_id,
                state=TrainingRunState.WAITING,
                next_step=next_step,
                base_artifact=spec.base_artifact,
                frozen_package_sha256=spec.frozen_package_sha256,
                training_material_sha256=spec.training_material_sha256,
                scale_authorization_sha256=spec.scale_authorization_sha256,
                execution_plan_sha256=execution_plan_sha256,
                job_fingerprint=fingerprint,
                candidate_artifact_ref=spec.candidate_artifact_ref,
                candidate_sha256=candidate_sha256,
                reason=decision.reason,
                queue_position=decision.queue_position,
            )

        try:
            for step_index in range(next_step, spec.max_steps):
                requested_control = _read_control(control)
                if requested_control is TrainingControl.PAUSE:
                    saved = self._save(
                        spec,
                        fingerprint=fingerprint,
                        state=TrainingRunState.PAUSED,
                        next_step=step_index,
                        resume_state=resume_state,
                        candidate_sha256=candidate_sha256,
                        reason="paused",
                    )
                    return self._evidence(
                        spec,
                        execution_plan_sha256=execution_plan_sha256,
                        job_fingerprint=fingerprint,
                        state=TrainingRunState.PAUSED,
                        next_step=step_index,
                        candidate_sha256=candidate_sha256,
                        checkpoint=saved,
                        reason="paused",
                    )
                if requested_control is TrainingControl.CANCEL:
                    saved = self._save(
                        spec,
                        fingerprint=fingerprint,
                        state=TrainingRunState.CANCELLED,
                        next_step=step_index,
                        resume_state=resume_state,
                        candidate_sha256=candidate_sha256,
                        reason="cancelled",
                    )
                    return self._evidence(
                        spec,
                        execution_plan_sha256=execution_plan_sha256,
                        job_fingerprint=fingerprint,
                        state=TrainingRunState.CANCELLED,
                        next_step=step_index,
                        candidate_sha256=candidate_sha256,
                        checkpoint=saved,
                        reason="cancelled",
                    )

                resource_decision = self._resources.revalidate(
                    scope=spec.resource_scope,
                    owner_id=spec.owner_id,
                    request_id=spec.job_id,
                )
                if not resource_decision.granted:
                    resource_reason = f"resource_revalidation:{resource_decision.reason}"
                    saved = self._save(
                        spec,
                        fingerprint=fingerprint,
                        state=TrainingRunState.PAUSED,
                        next_step=step_index,
                        resume_state=resume_state,
                        candidate_sha256=candidate_sha256,
                        reason=resource_reason,
                    )
                    return self._evidence(
                        spec,
                        execution_plan_sha256=execution_plan_sha256,
                        job_fingerprint=fingerprint,
                        state=TrainingRunState.PAUSED,
                        next_step=step_index,
                        candidate_sha256=candidate_sha256,
                        checkpoint=saved,
                        reason=resource_reason,
                    )

                self._save(
                    spec,
                    fingerprint=fingerprint,
                    state=TrainingRunState.DISPATCHING,
                    next_step=step_index,
                    resume_state=resume_state,
                    candidate_sha256=None,
                    reason=None,
                )
                if not _material_identity_matches(spec, self._training_materials):
                    saved = self._save(
                        spec,
                        fingerprint=fingerprint,
                        state=TrainingRunState.FAILED,
                        next_step=step_index,
                        resume_state=resume_state,
                        candidate_sha256=None,
                        reason="training_material_identity_mismatch",
                    )
                    return self._evidence(
                        spec,
                        execution_plan_sha256=execution_plan_sha256,
                        job_fingerprint=fingerprint,
                        state=TrainingRunState.FAILED,
                        next_step=step_index,
                        candidate_sha256=None,
                        checkpoint=saved,
                        reason="training_material_identity_mismatch",
                    )
                try:
                    self._training_materials.reverify()
                except TrainingMaterialResolutionError:
                    saved = self._save(
                        spec,
                        fingerprint=fingerprint,
                        state=TrainingRunState.FAILED,
                        next_step=step_index,
                        resume_state=resume_state,
                        candidate_sha256=None,
                        reason="training_material_verification_failed",
                    )
                    return self._evidence(
                        spec,
                        execution_plan_sha256=execution_plan_sha256,
                        job_fingerprint=fingerprint,
                        state=TrainingRunState.FAILED,
                        next_step=step_index,
                        candidate_sha256=None,
                        checkpoint=saved,
                        reason="training_material_verification_failed",
                    )
                try:
                    observed_execution_plan_sha256 = _worker_execution_plan_sha256(worker)
                except (TypeError, ValueError):
                    observed_execution_plan_sha256 = None
                if (
                    observed_execution_plan_sha256 is None
                    or not hmac.compare_digest(
                        observed_execution_plan_sha256,
                        execution_plan_sha256,
                    )
                ):
                    saved = self._save(
                        spec,
                        fingerprint=fingerprint,
                        state=TrainingRunState.FAILED,
                        next_step=step_index,
                        resume_state=resume_state,
                        candidate_sha256=None,
                        reason="training_execution_plan_changed",
                    )
                    return self._evidence(
                        spec,
                        execution_plan_sha256=execution_plan_sha256,
                        job_fingerprint=fingerprint,
                        state=TrainingRunState.FAILED,
                        next_step=step_index,
                        candidate_sha256=None,
                        checkpoint=saved,
                        reason="training_execution_plan_changed",
                    )
                try:
                    step_result = worker.step(
                        spec=spec,
                        step_index=step_index,
                        resume_state=dict(resume_state),
                        training_materials=self._training_materials,
                    )
                except TrainingWorkerError as exc:
                    failure_state = (
                        TrainingRunState.FAILED
                        if exc.effect is TrainingWorkerFailureEffect.NO_EFFECT
                        else TrainingRunState.RECONCILE_REQUIRED
                    )
                    reason = f"worker_{exc.effect.value}:{exc.code}"
                    saved = self._save(
                        spec,
                        fingerprint=fingerprint,
                        state=failure_state,
                        next_step=step_index,
                        resume_state=resume_state,
                        candidate_sha256=None,
                        reason=reason,
                    )
                    return self._evidence(
                        spec,
                        execution_plan_sha256=execution_plan_sha256,
                        job_fingerprint=fingerprint,
                        state=failure_state,
                        next_step=step_index,
                        candidate_sha256=None,
                        checkpoint=saved,
                        reason=reason,
                    )

                try:
                    if type(step_result) is not TrainingStepResult:
                        raise TypeError("worker result must be an exact TrainingStepResult")
                    canonical_result = TrainingStepResult(
                        resume_state=step_result.resume_state,
                        completed=step_result.completed,
                        candidate_sha256=step_result.candidate_sha256,
                    )
                except (TypeError, ValueError):
                    saved = self._save(
                        spec,
                        fingerprint=fingerprint,
                        state=TrainingRunState.RECONCILE_REQUIRED,
                        next_step=step_index,
                        resume_state=resume_state,
                        candidate_sha256=None,
                        reason="worker_unknown:invalid_result",
                    )
                    return self._evidence(
                        spec,
                        execution_plan_sha256=execution_plan_sha256,
                        job_fingerprint=fingerprint,
                        state=TrainingRunState.RECONCILE_REQUIRED,
                        next_step=step_index,
                        candidate_sha256=None,
                        checkpoint=saved,
                        reason="worker_unknown:invalid_result",
                    )

                resume_state = dict(canonical_result.resume_state)
                candidate_sha256 = canonical_result.candidate_sha256
                next_step = step_index + 1
                new_state = (
                    TrainingRunState.COMPLETED
                    if canonical_result.completed
                    else TrainingRunState.RUNNING
                )
                saved = self._save(
                    spec,
                    fingerprint=fingerprint,
                    state=new_state,
                    next_step=next_step,
                    resume_state=resume_state,
                    candidate_sha256=candidate_sha256,
                    reason=None,
                )
                if canonical_result.completed:
                    return self._evidence(
                        spec,
                        execution_plan_sha256=execution_plan_sha256,
                        job_fingerprint=fingerprint,
                        state=TrainingRunState.COMPLETED,
                        next_step=next_step,
                        candidate_sha256=candidate_sha256,
                        checkpoint=saved,
                    )

            saved = self._save(
                spec,
                fingerprint=fingerprint,
                state=TrainingRunState.EXHAUSTED,
                next_step=next_step,
                resume_state=resume_state,
                candidate_sha256=candidate_sha256,
                reason="max_steps_exhausted",
            )
            return self._evidence(
                spec,
                execution_plan_sha256=execution_plan_sha256,
                job_fingerprint=fingerprint,
                state=TrainingRunState.EXHAUSTED,
                next_step=next_step,
                candidate_sha256=candidate_sha256,
                checkpoint=saved,
                reason="max_steps_exhausted",
            )
        finally:
            self._resources.release(
                scope=spec.resource_scope,
                owner_id=spec.owner_id,
                request_id=spec.job_id,
            )

    @staticmethod
    def _evidence(
        spec: TrainingJobSpec,
        *,
        execution_plan_sha256: str,
        job_fingerprint: str,
        state: TrainingRunState,
        next_step: int,
        candidate_sha256: str | None,
        checkpoint: Checkpoint | None,
        reason: str | None = None,
    ) -> TrainingRunEvidence:
        return TrainingRunEvidence(
            job_id=spec.job_id,
            state=state,
            next_step=next_step,
            base_artifact=spec.base_artifact,
            frozen_package_sha256=spec.frozen_package_sha256,
            training_material_sha256=spec.training_material_sha256,
            scale_authorization_sha256=spec.scale_authorization_sha256,
            execution_plan_sha256=execution_plan_sha256,
            job_fingerprint=job_fingerprint,
            candidate_artifact_ref=spec.candidate_artifact_ref,
            candidate_sha256=candidate_sha256,
            checkpoint_id=None if checkpoint is None else checkpoint.checkpoint_id,
            reason=reason,
        )

    def _save(
        self,
        spec: TrainingJobSpec,
        *,
        fingerprint: str,
        state: TrainingRunState,
        next_step: int,
        resume_state: dict[str, object],
        candidate_sha256: str | None,
        reason: str | None,
    ) -> Checkpoint:
        payload: dict[str, object] = {
            "schema_version": _CHECKPOINT_SCHEMA_VERSION,
            "job_id": spec.job_id,
            "job_fingerprint": fingerprint,
            "frozen_package_sha256": spec.frozen_package_sha256,
            "training_material_sha256": spec.training_material_sha256,
            "scale_authorization_sha256": spec.scale_authorization_sha256,
            "next_step": next_step,
            "resume_state": resume_state,
            "candidate_artifact_ref": spec.candidate_artifact_ref,
            "candidate_sha256": candidate_sha256,
            "reason": reason,
        }
        return self._checkpoints.save(task_id=spec.task_id, stage=_stage(state), payload=payload)

    @staticmethod
    def _restore(
        *,
        spec: TrainingJobSpec,
        fingerprint: str,
        checkpoint: Checkpoint | None,
    ) -> tuple[TrainingRunState, int, dict[str, object], str | None, str | None]:
        if checkpoint is None:
            return TrainingRunState.RUNNING, 0, {}, None, None
        if not checkpoint.stage.startswith(_CHECKPOINT_PREFIX):
            if any(
                checkpoint.stage.startswith(prefix)
                for prefix in _LEGACY_CHECKPOINT_PREFIXES
            ):
                raise TrainingCheckpointError(
                    "legacy training checkpoint predates scale authorization"
                )
            raise TrainingCheckpointError(
                "latest task checkpoint is not training-runtime state; "
                "use a dedicated training task"
            )
        try:
            state = TrainingRunState(checkpoint.stage.removeprefix(_CHECKPOINT_PREFIX))
        except ValueError as exc:
            raise TrainingCheckpointError("unknown training checkpoint state") from exc
        if state is TrainingRunState.WAITING:
            raise TrainingCheckpointError(
                "WAITING must not be persisted as training execution state"
            )

        payload = checkpoint.payload
        if type(payload) is not dict:
            raise TrainingCheckpointError("invalid training checkpoint payload")
        if payload.get("schema_version") != _CHECKPOINT_SCHEMA_VERSION:
            raise TrainingCheckpointError("unsupported training checkpoint schema")
        if payload.get("job_id") != spec.job_id or payload.get("job_fingerprint") != fingerprint:
            raise TrainingCheckpointError("training checkpoint identity mismatch")
        if payload.get("frozen_package_sha256") != spec.frozen_package_sha256:
            raise TrainingCheckpointError("training frozen-package identity mismatch")
        if payload.get("training_material_sha256") != spec.training_material_sha256:
            raise TrainingCheckpointError("training material identity mismatch")
        if (
            payload.get("scale_authorization_sha256")
            != spec.scale_authorization_sha256
        ):
            raise TrainingCheckpointError("training scale authorization mismatch")
        if payload.get("candidate_artifact_ref") != spec.candidate_artifact_ref:
            raise TrainingCheckpointError("training candidate artifact identity mismatch")

        next_step = payload.get("next_step")
        resume_state = payload.get("resume_state")
        candidate_sha256 = payload.get("candidate_sha256")
        reason = payload.get("reason")
        if type(next_step) is not int or next_step < 0 or next_step > spec.max_steps:
            raise TrainingCheckpointError("invalid training checkpoint step")
        if reason is not None and type(reason) is not str:
            raise TrainingCheckpointError("invalid training checkpoint reason")
        if state in {
            TrainingRunState.PAUSED,
            TrainingRunState.CANCELLED,
            TrainingRunState.FAILED,
            TrainingRunState.DISPATCHING,
            TrainingRunState.RECONCILE_REQUIRED,
        } and next_step >= spec.max_steps:
            raise TrainingCheckpointError("training checkpoint step is inconsistent with state")
        if state is TrainingRunState.COMPLETED and next_step == 0:
            raise TrainingCheckpointError("completed training checkpoint has no completed step")
        if state is TrainingRunState.EXHAUSTED and next_step != spec.max_steps:
            raise TrainingCheckpointError("exhausted training checkpoint step is inconsistent")

        try:
            restored_step = TrainingStepResult(
                resume_state=resume_state,
                completed=state is TrainingRunState.COMPLETED,
                candidate_sha256=candidate_sha256,
            )
        except (TypeError, ValueError) as exc:
            raise TrainingCheckpointError("invalid training checkpoint result evidence") from exc

        return state, next_step, dict(restored_step.resume_state), candidate_sha256, reason
