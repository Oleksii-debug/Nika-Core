from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from nika_core.kernel.checkpoint import Checkpoint, CheckpointService
from nika_core.training_runtime.contracts import TrainingRunState
from nika_core.training_runtime.runtime import (
    _CHECKPOINT_PREFIX,
    _CHECKPOINT_SCHEMA_VERSION,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_REASON_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,191}$")


class TrainingStatusError(ValueError):
    """Raised when a durable checkpoint cannot be projected as trusted training status."""


@dataclass(frozen=True, slots=True)
class TrainingStatusProjection:
    """Privacy-minimized user-facing projection of one durable Loop-C checkpoint."""

    task_id: str
    state: TrainingRunState
    next_step: int
    checkpoint_id: str
    reason: str | None

    def render_text(self) -> str:
        reason = self.reason if self.reason is not None else "немає"
        return "\n".join(
            (
                "Стан навчання Nika.",
                f"Завдання: {self.task_id}",
                f"Стан: {self.state.value}",
                f"Наступний крок: {self.next_step}",
                f"Причина: {reason}",
                f"Checkpoint: {self.checkpoint_id}",
                (
                    "Обмеження доказовості: показано лише останній цілісний durable "
                    "training checkpoint цього task_id. Він не доводить реальний запуск "
                    "тренера, наявність ваг, успішне оцінювання, promotion, activation "
                    "або production readiness."
                ),
            )
        )


class TrainingStatusService:
    """Read one known training task through the canonical CheckpointService authority."""

    def __init__(self, checkpoints: CheckpointService) -> None:
        if type(checkpoints) is not CheckpointService:
            raise TypeError("checkpoints must be an exact CheckpointService")
        self._checkpoints = checkpoints

    def read(self, task_id: str) -> TrainingStatusProjection | None:
        canonical_task_id = _require_task_id(task_id)
        checkpoint = self._checkpoints.latest(canonical_task_id)
        if checkpoint is None:
            return None
        return _project_checkpoint(canonical_task_id, checkpoint)


def _require_task_id(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise TrainingStatusError("training task id must be a canonical UUID")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError) as exc:
        raise TrainingStatusError("training task id must be a canonical UUID") from exc
    if str(parsed) != value:
        raise TrainingStatusError("training task id must be a canonical UUID")
    return value


def _require_sha256(value: object, *, field_name: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise TrainingStatusError(f"invalid {field_name}")
    return value


def _project_checkpoint(task_id: str, checkpoint: Checkpoint) -> TrainingStatusProjection:
    if type(checkpoint) is not Checkpoint:
        raise TrainingStatusError("checkpoint must be an exact Checkpoint")
    if checkpoint.task_id != task_id:
        raise TrainingStatusError("training checkpoint task identity mismatch")
    if not checkpoint.stage.startswith(_CHECKPOINT_PREFIX):
        raise TrainingStatusError("latest checkpoint is not Loop-C training state")
    try:
        state = TrainingRunState(checkpoint.stage.removeprefix(_CHECKPOINT_PREFIX))
    except ValueError as exc:
        raise TrainingStatusError("unknown training checkpoint state") from exc
    if state is TrainingRunState.WAITING:
        raise TrainingStatusError("WAITING is not a persisted training execution state")

    payload = checkpoint.payload
    if type(payload) is not dict:
        raise TrainingStatusError("invalid training checkpoint payload")
    if payload.get("schema_version") != _CHECKPOINT_SCHEMA_VERSION:
        raise TrainingStatusError("unsupported training checkpoint schema")

    job_id = payload.get("job_id")
    candidate_artifact_ref = payload.get("candidate_artifact_ref")
    if (
        type(job_id) is not str
        or not job_id
        or len(job_id.encode("utf-8")) > 512
        or type(candidate_artifact_ref) is not str
        or not candidate_artifact_ref
        or len(candidate_artifact_ref.encode("utf-8")) > 512
    ):
        raise TrainingStatusError("invalid training checkpoint identity")
    _require_sha256(payload.get("job_fingerprint"), field_name="job fingerprint")
    _require_sha256(payload.get("frozen_package_sha256"), field_name="frozen package")
    _require_sha256(payload.get("training_material_sha256"), field_name="training material")

    next_step = payload.get("next_step")
    if type(next_step) is not int or next_step < 0 or next_step > 1_000_000:
        raise TrainingStatusError("invalid training checkpoint step")

    reason = payload.get("reason")
    if reason is not None and (type(reason) is not str or _REASON_RE.fullmatch(reason) is None):
        raise TrainingStatusError("invalid training checkpoint reason")

    candidate_sha256 = payload.get("candidate_sha256")
    if state is TrainingRunState.COMPLETED:
        _require_sha256(candidate_sha256, field_name="candidate result")
        if next_step == 0:
            raise TrainingStatusError("completed training checkpoint has no completed step")
    elif candidate_sha256 is not None:
        raise TrainingStatusError("non-completed training checkpoint published a candidate result")

    if type(payload.get("resume_state")) is not dict:
        raise TrainingStatusError("invalid training checkpoint resume state")

    try:
        checkpoint_uuid = UUID(checkpoint.checkpoint_id)
    except (ValueError, AttributeError) as exc:
        raise TrainingStatusError("invalid training checkpoint identity") from exc
    if str(checkpoint_uuid) != checkpoint.checkpoint_id:
        raise TrainingStatusError("invalid training checkpoint identity")

    return TrainingStatusProjection(
        task_id=task_id,
        state=state,
        next_step=next_step,
        checkpoint_id=checkpoint.checkpoint_id,
        reason=reason,
    )
