from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class TrainingControl(StrEnum):
    CONTINUE = "continue"
    PAUSE = "pause"
    CANCEL = "cancel"


class TrainingRunState(StrEnum):
    WAITING = "waiting"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    EXHAUSTED = "exhausted"


@dataclass(frozen=True, slots=True)
class ArtifactIdentity:
    """Immutable identity for an already-materialized model artifact."""

    artifact_ref: str
    sha256: str

    def __post_init__(self) -> None:
        if not self.artifact_ref.strip():
            raise ValueError("artifact_ref must not be empty")
        if not _SHA256_RE.fullmatch(self.sha256):
            raise ValueError("sha256 must be a lowercase 64-character hex digest")


@dataclass(frozen=True, slots=True)
class TrainingJobSpec:
    """Bounded Loop-C execution identity; policy and promotion live elsewhere."""

    job_id: str
    task_id: str
    project_id: str
    owner_id: str
    base_artifact: ArtifactIdentity
    candidate_artifact_ref: str
    max_steps: int
    resource_scope: str = "model_training"

    def __post_init__(self) -> None:
        for value, name in (
            (self.job_id, "job_id"),
            (self.task_id, "task_id"),
            (self.project_id, "project_id"),
            (self.owner_id, "owner_id"),
            (self.candidate_artifact_ref, "candidate_artifact_ref"),
            (self.resource_scope, "resource_scope"),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be empty")
        if self.max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        if self.candidate_artifact_ref == self.base_artifact.artifact_ref:
            raise ValueError("candidate artifact must not overwrite the base artifact")


@dataclass(frozen=True, slots=True)
class TrainingStepResult:
    """One bounded trainer step and the opaque state required for the next step."""

    resume_state: dict[str, object] = field(default_factory=dict)
    completed: bool = False
    candidate_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.candidate_sha256 is not None and not _SHA256_RE.fullmatch(self.candidate_sha256):
            raise ValueError("candidate_sha256 must be a lowercase 64-character hex digest")
        if self.completed and self.candidate_sha256 is None:
            raise ValueError("completed training must provide candidate_sha256")


@dataclass(frozen=True, slots=True)
class TrainingRunEvidence:
    job_id: str
    state: TrainingRunState
    next_step: int
    base_artifact: ArtifactIdentity
    candidate_artifact_ref: str
    candidate_sha256: str | None = None
    checkpoint_id: str | None = None
    reason: str | None = None
    queue_position: int | None = None


class TrainingWorkerPort(Protocol):
    """Adapter boundary for an actual trainer (PEFT/LoRA/etc.)."""

    def step(
        self,
        *,
        spec: TrainingJobSpec,
        step_index: int,
        resume_state: dict[str, object],
    ) -> TrainingStepResult: ...
