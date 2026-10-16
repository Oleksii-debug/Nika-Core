from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable


class RuntimeCapability(StrEnum):
    DETERMINISTIC_NO_LLM = "deterministic_no_llm"
    DURABLE_RESUME = "durable_resume"
    HUMAN_APPROVAL = "human_approval"
    CANCELLATION = "cancellation"
    PARALLELISM = "parallelism"
    SUBAGENTS = "subagents"
    MCP_TOOLS = "mcp_tools"
    LOCAL_MODELS = "local_models"


class RuntimeOutcome(StrEnum):
    COMPLETED = "completed"
    WAITING_APPROVAL = "waiting_approval"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    FAILED = "failed"


class RuntimeErrorCode(StrEnum):
    """Framework-neutral failure classes used by retry/safety policy."""

    TIMEOUT = "timeout"
    TRANSIENT = "transient"
    INVALID_RESUME = "invalid_resume"
    RESUME_UNAVAILABLE = "resume_unavailable"
    DUPLICATE_ACTIVE = "duplicate_active"
    INTERNAL = "internal"


class RuntimeResumeMode(StrEnum):
    CONTINUE = "continue"
    APPROVAL = "approval"


class RuntimeResumeProbeStatus(StrEnum):
    """Framework-neutral durability verdict for one persisted resume cursor."""

    READY = "ready"
    MISSING = "missing"
    UNREADABLE = "unreadable"
    UNVERIFIABLE = "unverifiable"
    INVALID = "invalid"


class RuntimeUnsupportedError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RuntimeRequest:
    task_id: str
    thread_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    max_steps: int = 64
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise ValueError("task_id must not be empty")
        if not self.thread_id.strip():
            raise ValueError("thread_id must not be empty")
        if self.max_steps < 1:
            raise ValueError("max_steps must be positive")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive when provided")


@dataclass(frozen=True, slots=True)
class RuntimeResumeRequest:
    task_id: str
    thread_id: str
    resume_token: str
    mode: RuntimeResumeMode = RuntimeResumeMode.CONTINUE
    value: Any = None
    max_steps: int = 64
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        if (
            type(self.task_id) is not str
            or type(self.thread_id) is not str
            or type(self.resume_token) is not str
        ):
            raise TypeError("resume identifiers must be exact strings")
        if not self.task_id.strip() or not self.thread_id.strip() or not self.resume_token.strip():
            raise ValueError("resume identifiers must not be empty")
        if self.max_steps < 1:
            raise ValueError("max_steps must be positive")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive when provided")


_MAX_RESUME_PROBE_REASON_CHARS = 1024
_MAX_RESUME_CHECKPOINT_ID_CHARS = 1024


@dataclass(frozen=True, slots=True)
class RuntimeResumeProbe:
    """Nika-owned verdict proving whether a persisted runtime cursor is safe to resume."""

    status: RuntimeResumeProbeStatus
    reason: str
    checkpoint_id: str | None = None

    def __post_init__(self) -> None:
        if type(self.status) is not RuntimeResumeProbeStatus:
            raise TypeError("resume probe status must be an exact RuntimeResumeProbeStatus")
        if type(self.reason) is not str:
            raise TypeError("resume probe reason must be an exact string")
        if not self.reason.strip():
            raise ValueError("resume probe reason must not be empty")
        if len(self.reason) > _MAX_RESUME_PROBE_REASON_CHARS:
            raise ValueError("resume probe reason is too long")
        if self.checkpoint_id is not None:
            if type(self.checkpoint_id) is not str:
                raise TypeError("checkpoint_id must be an exact string when provided")
            if not self.checkpoint_id.strip():
                raise ValueError("checkpoint_id must not be empty")
            if self.checkpoint_id != self.checkpoint_id.strip():
                raise ValueError("checkpoint_id must not have surrounding whitespace")
            if len(self.checkpoint_id) > _MAX_RESUME_CHECKPOINT_ID_CHARS:
                raise ValueError("checkpoint_id is too long")
            if any(ord(char) < 32 or ord(char) == 127 for char in self.checkpoint_id):
                raise ValueError("checkpoint_id must not contain control characters")
        if self.status is RuntimeResumeProbeStatus.READY and self.checkpoint_id is None:
            raise ValueError("ready resume probe requires checkpoint_id")

    @property
    def can_resume(self) -> bool:
        return self.status is RuntimeResumeProbeStatus.READY


def canonical_resume_probe(value: object) -> RuntimeResumeProbe:
    """Snapshot untrusted adapter probe evidence through Nika's canonical constructor."""

    if type(value) is not RuntimeResumeProbe:
        raise TypeError("runtime resume probe must be an exact RuntimeResumeProbe")
    try:
        status = object.__getattribute__(value, "status")
        reason = object.__getattribute__(value, "reason")
        checkpoint_id = object.__getattribute__(value, "checkpoint_id")
    except AttributeError as exc:
        raise ValueError("runtime resume probe is incomplete") from exc
    return RuntimeResumeProbe(
        status=status,
        reason=reason,
        checkpoint_id=checkpoint_id,
    )


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    sequence: int
    event_type: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.sequence < 0:
            raise ValueError("sequence must not be negative")
        if not self.event_type.strip():
            raise ValueError("event_type must not be empty")


@dataclass(frozen=True, slots=True)
class RuntimeResult:
    outcome: RuntimeOutcome
    events: tuple[RuntimeEvent, ...] = ()
    output: Mapping[str, Any] = field(default_factory=dict)
    resume_token: str | None = None
    error: str | None = None
    error_code: RuntimeErrorCode | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.outcome, RuntimeOutcome):
            raise TypeError("outcome must be a RuntimeOutcome")
        if isinstance(self.resume_token, str) and type(self.resume_token) is not str:
            raise TypeError("resume_token must be an exact string when provided")
        if (
            self.outcome in {RuntimeOutcome.WAITING_APPROVAL, RuntimeOutcome.PAUSED}
            and (type(self.resume_token) is not str or not self.resume_token.strip())
        ):
            raise ValueError("resumable outcome requires a usable resume token")
        if self.outcome == RuntimeOutcome.FAILED and not self.error:
            raise ValueError("failed outcome requires an error")
        if self.error_code is not None and not isinstance(self.error_code, RuntimeErrorCode):
            raise TypeError("error_code must be a RuntimeErrorCode when provided")
        if self.outcome != RuntimeOutcome.FAILED and self.error_code is not None:
            raise ValueError("error_code is only valid for failed outcomes")


@runtime_checkable
class AgentRuntimePort(Protocol):
    @property
    def runtime_id(self) -> str: ...

    @property
    def capabilities(self) -> frozenset[RuntimeCapability]: ...

    async def run(self, request: RuntimeRequest) -> RuntimeResult: ...

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult: ...

    async def cancel(self, *, task_id: str, thread_id: str) -> bool: ...


@runtime_checkable
class RuntimeResumeProbePort(Protocol):
    """Optional durability extension used before automatic or framework resume."""

    async def probe_resume(
        self,
        *,
        task_id: str,
        thread_id: str,
        resume_token: str,
    ) -> RuntimeResumeProbe: ...
