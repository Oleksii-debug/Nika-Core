from __future__ import annotations

import json
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from math import isfinite
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


def _require_exact_nonempty_text(value: object, *, field_name: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be an exact string")
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")


def _require_positive_step_count(value: object) -> None:
    if type(value) is not int:
        raise TypeError("max_steps must be an exact integer")
    if value < 1:
        raise ValueError("max_steps must be positive")


def _require_optional_positive_finite_timeout(value: object) -> None:
    if value is None:
        return
    if type(value) not in (int, float):
        raise TypeError("timeout_seconds must be numeric")
    try:
        finite = isfinite(float(value))
    except OverflowError:
        finite = False
    if not finite or value <= 0:
        raise ValueError("timeout_seconds must be finite and positive")


@dataclass(frozen=True, slots=True)
class RuntimeRequest:
    task_id: str
    thread_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    max_steps: int = 64
    timeout_seconds: float | None = None

    def __post_init__(self) -> None:
        _require_exact_nonempty_text(self.task_id, field_name="task_id")
        _require_exact_nonempty_text(self.thread_id, field_name="thread_id")
        _require_positive_step_count(self.max_steps)
        _require_optional_positive_finite_timeout(self.timeout_seconds)


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
        if type(self.mode) is not RuntimeResumeMode:
            raise TypeError("mode must be a RuntimeResumeMode")
        _require_positive_step_count(self.max_steps)
        _require_optional_positive_finite_timeout(self.timeout_seconds)


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
        try:
            self.reason.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("resume probe reason must be valid UTF-8") from None
        if self.checkpoint_id is not None:
            if type(self.checkpoint_id) is not str:
                raise TypeError("checkpoint_id must be an exact string when provided")
            if not self.checkpoint_id.strip():
                raise ValueError("checkpoint_id must not be empty")
            if self.checkpoint_id != self.checkpoint_id.strip():
                raise ValueError("checkpoint_id must not have surrounding whitespace")
            if len(self.checkpoint_id) > _MAX_RESUME_CHECKPOINT_ID_CHARS:
                raise ValueError("checkpoint_id is too long")
            try:
                self.checkpoint_id.encode("utf-8")
            except UnicodeEncodeError:
                raise ValueError("checkpoint_id must be valid UTF-8") from None
            if any(
                unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"}
                for char in self.checkpoint_id
            ):
                raise ValueError("checkpoint_id must not contain control or format characters")
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
        if type(self.sequence) is not int:
            raise TypeError("sequence must be an exact integer")
        if self.sequence < 0:
            raise ValueError("sequence must not be negative")
        _require_exact_nonempty_text(self.event_type, field_name="event_type")


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


# Control/audit events are emitted only by Nika, never by an adapter result.
# Update this set when the coordinator/recovery service adds an authoritative event.
_NIKA_OWNED_RUNTIME_AUDIT_EVENTS = frozenset(
    {
        "runtime.started",
        "runtime.session_bound",
        "runtime.retry_blocked_unsafe_fresh_replay",
        "runtime.retry_blocked_timeout_budget",
        "runtime.retry_scheduled",
        "runtime.retry_blocked_cancelled",
        "runtime.retry_started",
        "runtime.approval_resumed",
        "runtime.saved_resume_started",
        "runtime.saved_approval_resumed",
        "runtime.cancel_requested",
        "runtime.cancel_accepted",
        "runtime.cancel_uncertain",
        "runtime.cancel_not_active",
        "runtime.crash_recovery_started",
        "runtime.recovery_claim_acquired",
        "runtime.recovery_claim_reclaimed",
        "runtime.recovery_effect_started",
        "runtime.finished_after_cancel",
        "runtime.recovery_claim_completed",
        "runtime.finished",
        "runtime.recovery_inventory",
        "runtime.recovery_checkpoint_blocked",
        "runtime.recovery_auto_resume_requested",
        "runtime.recovery_auto_resume_failed",
    }
)


def _require_json_string_keys(value: Any, *, field_name: str) -> None:
    """Reject nested keys that JSON would otherwise silently coerce to strings."""

    if isinstance(value, dict):
        for key, item in dict.items(value):
            if type(key) is not str:
                raise TypeError(f"{field_name} keys must be exact strings")
            _require_json_string_keys(item, field_name=field_name)
    elif isinstance(value, list):
        for item in list.__iter__(value):
            _require_json_string_keys(item, field_name=field_name)
    elif isinstance(value, tuple):
        for item in tuple.__iter__(value):
            _require_json_string_keys(item, field_name=field_name)


def _snapshot_json_mapping(value: Mapping[str, Any], *, field_name: str) -> dict[str, Any]:
    """Copy adapter output into JSON-safe, detached Nika-owned values."""

    if not isinstance(value, Mapping):
        raise TypeError(f"{field_name} must be a mapping")
    copied = dict(value)
    _require_json_string_keys(copied, field_name=field_name)
    encoded = json.dumps(copied, ensure_ascii=False, allow_nan=False, sort_keys=True)
    # SQLite and the Windows JSON transport cannot store unpaired surrogates.
    encoded.encode("utf-8")
    return json.loads(encoded)


def canonical_runtime_result(value: object) -> RuntimeResult:
    """Snapshot untrusted adapter evidence before it changes Nika's durable state."""

    if type(value) is not RuntimeResult:
        raise TypeError("runtime adapter must return an exact RuntimeResult")
    try:
        outcome = object.__getattribute__(value, "outcome")
        events = object.__getattribute__(value, "events")
        output = object.__getattribute__(value, "output")
        resume_token = object.__getattribute__(value, "resume_token")
        error = object.__getattribute__(value, "error")
        error_code = object.__getattribute__(value, "error_code")
    except AttributeError:
        raise ValueError("runtime adapter result is incomplete") from None

    if type(events) is not tuple:
        raise TypeError("runtime result events must be a tuple")
    if not isinstance(output, Mapping):
        raise TypeError("runtime result output must be a mapping")
    if resume_token is not None and type(resume_token) is not str:
        raise TypeError("runtime result resume token must be an exact string")
    if error is not None and type(error) is not str:
        raise TypeError("runtime result error must be an exact string")
    if error_code is not None and type(error_code) is not RuntimeErrorCode:
        raise TypeError("runtime result error code must be exact")
    if resume_token is not None:
        resume_token.encode("utf-8")
    if error is not None:
        error.encode("utf-8")
    canonical_output = _snapshot_json_mapping(output, field_name="runtime result output")

    canonical_events = []
    for event in events:
        if type(event) is not RuntimeEvent:
            raise TypeError("runtime result contains an invalid event")
        sequence = object.__getattribute__(event, "sequence")
        event_type = object.__getattribute__(event, "event_type")
        if type(sequence) is not int or sequence < 0:
            raise ValueError("runtime event sequence must be a non-negative integer")
        if type(event_type) is not str or not event_type.strip():
            raise ValueError("runtime event type must be an exact nonempty string")
        event_type.encode("utf-8")
        if any(
            unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"}
            for char in event_type
        ):
            raise ValueError("runtime event type contains control or formatting characters")
        if event_type in _NIKA_OWNED_RUNTIME_AUDIT_EVENTS:
            raise ValueError("runtime adapter cannot impersonate Nika-owned audit events")
        event_payload = _snapshot_json_mapping(
            object.__getattribute__(event, "payload"), field_name="runtime event payload"
        )
        if "sequence" in event_payload:
            raise ValueError("runtime event payload must not override the authoritative sequence")
        canonical_events.append(
            RuntimeEvent(sequence=sequence, event_type=event_type, payload=event_payload)
        )

    return RuntimeResult(
        outcome=outcome,
        events=tuple(canonical_events),
        output=canonical_output,
        resume_token=resume_token,
        error=error,
        error_code=error_code,
    )


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
