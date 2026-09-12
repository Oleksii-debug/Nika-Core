from __future__ import annotations

import hashlib
import json
from collections.abc import Callable

from nika_core.kernel.checkpoint import Checkpoint, CheckpointService
from nika_core.resources.manager import ResourceManager
from nika_core.training_runtime.contracts import (
    TrainingControl,
    TrainingJobSpec,
    TrainingRunEvidence,
    TrainingRunState,
    TrainingWorkerPort,
)

_CHECKPOINT_PREFIX = "training_runtime/v1/"
_TERMINAL_STATES = {
    TrainingRunState.COMPLETED,
    TrainingRunState.CANCELLED,
    TrainingRunState.EXHAUSTED,
}


class TrainingCheckpointError(RuntimeError):
    """Raised when durable training state cannot be trusted for safe resume."""


def _job_fingerprint(spec: TrainingJobSpec) -> str:
    identity = {
        "job_id": spec.job_id,
        "task_id": spec.task_id,
        "project_id": spec.project_id,
        "owner_id": spec.owner_id,
        "base_artifact_ref": spec.base_artifact.artifact_ref,
        "base_sha256": spec.base_artifact.sha256,
        "candidate_artifact_ref": spec.candidate_artifact_ref,
        "max_steps": spec.max_steps,
        "resource_scope": spec.resource_scope,
    }
    body = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _stage(state: TrainingRunState) -> str:
    return f"{_CHECKPOINT_PREFIX}{state.value}"


class TrainingRuntime:
    """Executes bounded model-training work without owning scheduling or promotion policy.

    The supplied task must be dedicated to this training job. Durable checkpoints use the
    canonical task checkpoint store; ResourceManager remains the sole admission authority.
    """

    def __init__(self, *, resources: ResourceManager, checkpoints: CheckpointService) -> None:
        self._resources = resources
        self._checkpoints = checkpoints

    def run(
        self,
        spec: TrainingJobSpec,
        worker: TrainingWorkerPort,
        *,
        control: Callable[[], TrainingControl] | None = None,
    ) -> TrainingRunEvidence:
        control = control or (lambda: TrainingControl.CONTINUE)
        fingerprint = _job_fingerprint(spec)
        checkpoint = self._checkpoints.latest(spec.task_id)
        state, next_step, resume_state, candidate_sha256, reason = self._restore(
            spec=spec,
            fingerprint=fingerprint,
            checkpoint=checkpoint,
        )

        if state in _TERMINAL_STATES:
            return self._evidence(
                spec,
                state=state,
                next_step=next_step,
                candidate_sha256=candidate_sha256,
                checkpoint=checkpoint,
                reason=reason,
            )

        requested_control = control()
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
                candidate_artifact_ref=spec.candidate_artifact_ref,
                candidate_sha256=candidate_sha256,
                reason=decision.reason,
                queue_position=decision.queue_position,
            )

        try:
            for step_index in range(next_step, spec.max_steps):
                requested_control = control()
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
                        state=TrainingRunState.CANCELLED,
                        next_step=step_index,
                        candidate_sha256=candidate_sha256,
                        checkpoint=saved,
                        reason="cancelled",
                    )

                try:
                    step_result = worker.step(
                        spec=spec,
                        step_index=step_index,
                        resume_state=dict(resume_state),
                    )
                except Exception as exc:  # adapter failures become durable resumable evidence
                    saved = self._save(
                        spec,
                        fingerprint=fingerprint,
                        state=TrainingRunState.FAILED,
                        next_step=step_index,
                        resume_state=resume_state,
                        candidate_sha256=candidate_sha256,
                        reason=f"worker_error:{type(exc).__name__}",
                    )
                    return self._evidence(
                        spec,
                        state=TrainingRunState.FAILED,
                        next_step=step_index,
                        candidate_sha256=candidate_sha256,
                        checkpoint=saved,
                        reason=f"worker_error:{type(exc).__name__}",
                    )

                resume_state = dict(step_result.resume_state)
                candidate_sha256 = step_result.candidate_sha256 or candidate_sha256
                next_step = step_index + 1
                new_state = (
                    TrainingRunState.COMPLETED
                    if step_result.completed
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
                if step_result.completed:
                    return self._evidence(
                        spec,
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
            "schema_version": 1,
            "job_id": spec.job_id,
            "job_fingerprint": fingerprint,
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
            raise TrainingCheckpointError(
                "latest task checkpoint is not training-runtime state; use a dedicated training task"
            )
        try:
            state = TrainingRunState(checkpoint.stage.removeprefix(_CHECKPOINT_PREFIX))
        except ValueError as exc:
            raise TrainingCheckpointError("unknown training checkpoint state") from exc

        payload = checkpoint.payload
        if payload.get("schema_version") != 1:
            raise TrainingCheckpointError("unsupported training checkpoint schema")
        if payload.get("job_id") != spec.job_id or payload.get("job_fingerprint") != fingerprint:
            raise TrainingCheckpointError("training checkpoint identity mismatch")
        if payload.get("candidate_artifact_ref") != spec.candidate_artifact_ref:
            raise TrainingCheckpointError("training candidate artifact identity mismatch")

        next_step = payload.get("next_step")
        resume_state = payload.get("resume_state")
        candidate_sha256 = payload.get("candidate_sha256")
        reason = payload.get("reason")
        if not isinstance(next_step, int) or next_step < 0 or next_step > spec.max_steps:
            raise TrainingCheckpointError("invalid training checkpoint step")
        if not isinstance(resume_state, dict):
            raise TrainingCheckpointError("invalid training checkpoint resume state")
        if candidate_sha256 is not None and not isinstance(candidate_sha256, str):
            raise TrainingCheckpointError("invalid training checkpoint candidate digest")
        if reason is not None and not isinstance(reason, str):
            raise TrainingCheckpointError("invalid training checkpoint reason")

        return state, next_step, dict(resume_state), candidate_sha256, reason
