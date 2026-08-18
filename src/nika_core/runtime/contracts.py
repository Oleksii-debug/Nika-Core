from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping, Protocol, runtime_checkable


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
    DUPLICATE_ACTIVE = "duplicate_active"
    INTERNAL = "internal"


class RuntimeResumeMode(StrEnum):
    CONTINUE = "continue"
    APPROVAL = "approval"


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
        if not self.task_id.strip() or not self.thread_id.strip() or not self.resume_token.strip():
            raise ValueError("resume identifiers must not be empty")
        if self.max_steps < 1:
            raise ValueError("max_steps must be positive")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive when provided")


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
        if self.outcome == RuntimeOutcome.WAITING_APPROVAL and not self.resume_token:
            raise ValueError("waiting approval requires a resume token")
        if self.outcome == RuntimeOutcome.FAILED and not self.error:
            raise ValueError("failed outcome requires an error")
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
