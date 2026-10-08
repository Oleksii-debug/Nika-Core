from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from math import isfinite
from typing import Any, Protocol, runtime_checkable
from unicodedata import category, is_normalized


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


MAX_RUNTIME_STEPS = 10_000
# Maximum exactly representable JSON integer in JavaScript and safe within SQLite int64.
MAX_RUNTIME_EVENT_SEQUENCE = (1 << 53) - 1
MAX_RUNTIME_TIMEOUT_SECONDS = 86_400
MAX_RUNTIME_ID_UTF8_BYTES = 512
MAX_RUNTIME_PROBE_REASON_UTF8_BYTES = 2048
MAX_RUNTIME_RESULT_ERROR_UTF8_BYTES = 4096


def _validate_runtime_identity(value: str, field_name: str) -> None:
    """Keep durable runtime identifiers canonical, bounded, before admission or recovery."""
    if type(value) is not str:
        raise TypeError(f"{field_name} must be a string")
    # Bound code points before any O(n) Unicode normalization or encoding work.
    if len(value) > MAX_RUNTIME_ID_UTF8_BYTES:
        raise ValueError(f"{field_name} exceeds the durable identity size limit")
    try:
        byte_count = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError(f"{field_name} contains invalid Unicode") from None
    if byte_count > MAX_RUNTIME_ID_UTF8_BYTES:
        raise ValueError(f"{field_name} exceeds the durable identity size limit")
    if not value or value != value.strip():
        raise ValueError(f"{field_name} must be nonempty canonical text")
    if not is_normalized("NFC", value) or any(
        category(character) in {"Cc", "Cf", "Cs"} or character in "\u0085\u2028\u2029"
        for character in value
    ):
        raise ValueError(f"{field_name} contains noncanonical or control text")


def _validate_probe_reason(value: str) -> None:
    """Bound and canonicalize diagnostic text before restart/audit presentation."""
    if type(value) is not str:
        raise TypeError("resume probe reason must be a plain string")
    if len(value) > MAX_RUNTIME_PROBE_REASON_UTF8_BYTES:
        raise ValueError("resume probe reason exceeds diagnostic size limit")
    try:
        encoded_size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("resume probe reason contains invalid Unicode") from None
    if encoded_size > MAX_RUNTIME_PROBE_REASON_UTF8_BYTES:
        raise ValueError("resume probe reason exceeds diagnostic size limit")
    if not value or value != value.strip():
        raise ValueError("resume probe reason must be nonempty canonical text")
    if not is_normalized("NFC", value) or any(
        category(character) in {"Cc", "Cf", "Cs"} or character in "\u0085\u2028\u2029"
        for character in value
    ):
        raise ValueError("resume probe reason contains noncanonical or control text")


def _validate_runtime_result_error(value: str) -> None:
    """Keep provider diagnostics inert, bounded and readable across durable readback."""
    if type(value) is not str:
        raise TypeError("runtime error must be a plain string")
    if len(value) > MAX_RUNTIME_RESULT_ERROR_UTF8_BYTES:
        raise ValueError("runtime error exceeds diagnostic size limit")
    try:
        byte_count = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        raise ValueError("runtime error contains invalid Unicode") from None
    if byte_count > MAX_RUNTIME_RESULT_ERROR_UTF8_BYTES:
        raise ValueError("runtime error exceeds diagnostic size limit")
    # Existing multiline/tab diagnostics remain valid, but no terminal escapes,
    # bidi overrides, carriage-return overwrites or surrogate code points.
    if not is_normalized("NFC", value) or any(
        (category(char) in {"Cf", "Cs", "Cc"} and char not in "\n\t")
        or char in "\u0085\u2028\u2029"
        for char in value
    ):
        raise ValueError("runtime error contains noncanonical or control text")


def _validate_limits(max_steps: int, timeout_seconds: float | None) -> None:
    """Reject malformed budgets before they reach provider/runtime effects."""
    if type(max_steps) is not int:
        raise TypeError("max_steps must be a plain integer, not bool or float")
    if not 1 <= max_steps <= MAX_RUNTIME_STEPS:
        raise ValueError(f"max_steps must be between 1 and {MAX_RUNTIME_STEPS}")
    if timeout_seconds is not None:
        if type(timeout_seconds) not in (int, float):
            raise TypeError("timeout_seconds must be a plain number when provided")
        try:
            finite = isfinite(timeout_seconds)
        except OverflowError:
            finite = False
        if not finite or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive when provided")
        if timeout_seconds > MAX_RUNTIME_TIMEOUT_SECONDS:
            raise ValueError(
                f"timeout_seconds must not exceed {MAX_RUNTIME_TIMEOUT_SECONDS}"
            )


@dataclass(frozen=True, slots=True)
class RuntimeRequest:
    task_id: str
    thread_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    max_steps: int = 64
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        _validate_runtime_identity(self.task_id, "task_id")
        _validate_runtime_identity(self.thread_id, "thread_id")
        _validate_limits(self.max_steps, self.timeout_seconds)
        if not isinstance(self.payload, Mapping):
            raise TypeError("runtime request payload must be a mapping")


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
        _validate_runtime_identity(self.task_id, "task_id")
        _validate_runtime_identity(self.thread_id, "thread_id")
        _validate_runtime_identity(self.resume_token, "resume_token")
        if not isinstance(self.mode, RuntimeResumeMode):
            raise TypeError("mode must be a RuntimeResumeMode")
        _validate_limits(self.max_steps, self.timeout_seconds)


@dataclass(frozen=True, slots=True)
class RuntimeResumeProbe:
    """Nika-owned verdict proving whether a persisted runtime cursor is safe to resume."""

    status: RuntimeResumeProbeStatus
    reason: str
    checkpoint_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, RuntimeResumeProbeStatus):
            raise TypeError("resume probe status must be a RuntimeResumeProbeStatus")
        _validate_probe_reason(self.reason)
        if self.checkpoint_id is not None:
            _validate_runtime_identity(self.checkpoint_id, "checkpoint_id")
        if self.status == RuntimeResumeProbeStatus.READY and self.checkpoint_id is None:
            raise ValueError("ready resume probe requires checkpoint_id")

    @property
    def can_resume(self) -> bool:
        return self.status == RuntimeResumeProbeStatus.READY


@dataclass(frozen=True, slots=True)
class RuntimeEvent:
    sequence: int
    event_type: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.sequence) is not int:
            raise TypeError("event sequence must be a plain integer")
        if self.sequence < 0:
            raise ValueError("sequence must not be negative")
        if self.sequence > MAX_RUNTIME_EVENT_SEQUENCE:
            raise ValueError("event sequence exceeds portable integer limit")
        _validate_runtime_identity(self.event_type, "event_type")
        if not isinstance(self.payload, Mapping):
            raise TypeError("runtime event payload must be a mapping")


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
        # Dataclass type hints alone do not validate provider-supplied event or
        # output containers. Reject malformed carriers before durable consumers.
        if type(self.events) is not tuple or any(
            type(event) is not RuntimeEvent for event in self.events
        ):
            raise TypeError("runtime events must be a tuple of RuntimeEvent")
        if not isinstance(self.output, Mapping):
            raise TypeError("runtime output must be a mapping")
        if self.resume_token is not None:
            _validate_runtime_identity(self.resume_token, "resume_token")
        if (
            self.outcome in {RuntimeOutcome.WAITING_APPROVAL, RuntimeOutcome.PAUSED}
            and self.resume_token is None
        ):
            raise ValueError("resumable outcome requires a usable resume token")
        if self.error is not None:
            _validate_runtime_result_error(self.error)
        if self.outcome == RuntimeOutcome.FAILED and (
            self.error is None or not self.error.strip()
        ):
            raise ValueError("failed outcome requires a nonempty error")
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
