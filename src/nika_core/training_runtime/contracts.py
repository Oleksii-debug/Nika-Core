from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MACHINE_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,127}$")
_MAX_IDENTIFIER_BYTES = 512
_MAX_STEPS = 1_000_000
_MAX_RESUME_STATE_BYTES = 64 * 1024
_MAX_JSON_DEPTH = 12
_MAX_JSON_NODES = 4096


def _require_bounded_text(value: object, *, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty canonical string")
    if len(value.encode("utf-8")) > _MAX_IDENTIFIER_BYTES:
        raise ValueError(f"{name} exceeds the configured byte limit")
    return value


def _require_sha256(value: object, *, name: str) -> str:
    if type(value) is not str or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase 64-character hex digest")
    return value


def _copy_bounded_resume_state(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise ValueError("resume_state must be an exact JSON object")

    nodes = 0

    def visit(item: object, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_JSON_NODES:
            raise ValueError("resume_state contains too many JSON values")
        if depth > _MAX_JSON_DEPTH:
            raise ValueError("resume_state exceeds the JSON nesting limit")
        if item is None or type(item) in (bool, int, str):
            return
        if type(item) is float:
            if not math.isfinite(item):
                raise ValueError("resume_state contains a non-finite number")
            return
        if type(item) is list:
            for child in item:
                visit(child, depth + 1)
            return
        if type(item) is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise ValueError("resume_state contains a non-string JSON key")
                visit(child, depth + 1)
            return
        raise ValueError("resume_state contains a non-JSON value")

    visit(value, 0)
    try:
        encoded = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("resume_state is not valid canonical JSON") from exc
    if len(encoded) > _MAX_RESUME_STATE_BYTES:
        raise ValueError("resume_state exceeds the configured byte limit")
    decoded = json.loads(encoded)
    if type(decoded) is not dict:  # pragma: no cover - guarded above
        raise ValueError("resume_state must be a JSON object")
    return decoded


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
    RECONCILE_REQUIRED = "reconcile_required"
    EXHAUSTED = "exhausted"


class TrainingWorkerFailureEffect(StrEnum):
    """Whether a failed worker call can be proven to have caused no external effect."""

    NO_EFFECT = "no_effect"
    UNKNOWN = "unknown"


class TrainingWorkerError(RuntimeError):
    """Bounded worker failure with explicit external-effect truth."""

    def __init__(
        self,
        code: str,
        *,
        effect: TrainingWorkerFailureEffect,
    ) -> None:
        if type(code) is not str or not _MACHINE_CODE_RE.fullmatch(code):
            raise ValueError("worker error code must be a bounded machine code")
        if type(effect) is not TrainingWorkerFailureEffect:
            raise TypeError("worker failure effect must be TrainingWorkerFailureEffect")
        self.code = code
        self.effect = effect
        super().__init__(f"training worker failed: {code}")


@dataclass(frozen=True, slots=True)
class ArtifactIdentity:
    """Immutable identity for an already-materialized model artifact."""

    artifact_ref: str
    sha256: str

    def __post_init__(self) -> None:
        _require_bounded_text(self.artifact_ref, name="artifact_ref")
        _require_sha256(self.sha256, name="sha256")


@dataclass(frozen=True, slots=True)
class TrainingJobSpec:
    """Bounded Loop-C execution identity; policy and promotion live elsewhere."""

    job_id: str
    task_id: str
    project_id: str
    owner_id: str
    base_artifact: ArtifactIdentity
    frozen_package_sha256: str
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
            _require_bounded_text(value, name=name)
        if type(self.base_artifact) is not ArtifactIdentity:
            raise TypeError("base_artifact must be an exact ArtifactIdentity")
        _require_sha256(self.frozen_package_sha256, name="frozen_package_sha256")
        if type(self.max_steps) is not int or not 1 <= self.max_steps <= _MAX_STEPS:
            raise ValueError(f"max_steps must be an integer from 1 through {_MAX_STEPS}")
        if self.candidate_artifact_ref == self.base_artifact.artifact_ref:
            raise ValueError("candidate artifact must not overwrite the base artifact")


@dataclass(frozen=True, slots=True)
class TrainingStepResult:
    """One bounded trainer step and the opaque state required for the next step."""

    resume_state: dict[str, object] = field(default_factory=dict)
    completed: bool = False
    candidate_sha256: str | None = None

    def __post_init__(self) -> None:
        normalized_state = _copy_bounded_resume_state(self.resume_state)
        object.__setattr__(self, "resume_state", normalized_state)
        if type(self.completed) is not bool:
            raise ValueError("completed must be an exact boolean")
        if self.candidate_sha256 is not None:
            _require_sha256(self.candidate_sha256, name="candidate_sha256")
        if self.completed and self.candidate_sha256 is None:
            raise ValueError("completed training must provide candidate_sha256")
        if not self.completed and self.candidate_sha256 is not None:
            raise ValueError("incomplete training must not publish candidate_sha256")


@dataclass(frozen=True, slots=True)
class TrainingRunEvidence:
    job_id: str
    state: TrainingRunState
    next_step: int
    base_artifact: ArtifactIdentity
    frozen_package_sha256: str
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
