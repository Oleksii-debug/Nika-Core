from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from math import isfinite

from nika_core.runtime.contracts import RuntimeErrorCode, RuntimeOutcome, RuntimeResult


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Explicit, fail-closed retry policy for runtime failures.

    Retries are disabled by default. A caller must opt into exact error codes. By default
    a failed invocation must also expose a resume token so the coordinator can continue
    from durable runtime state instead of blindly replaying the original input.
    """

    max_retries: int = 0
    retryable_error_codes: frozenset[RuntimeErrorCode] = field(default_factory=frozenset)
    base_delay_seconds: float = 0.0
    max_delay_seconds: float = 30.0
    allow_fresh_retry: bool = False

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError("max_retries must not be negative")
        if self.base_delay_seconds < 0:
            raise ValueError("base_delay_seconds must not be negative")
        if self.max_delay_seconds < 0:
            raise ValueError("max_delay_seconds must not be negative")
        if self.base_delay_seconds > self.max_delay_seconds:
            raise ValueError("base_delay_seconds must not exceed max_delay_seconds")

    def should_retry(self, result: RuntimeResult, *, retries_used: int) -> bool:
        if retries_used >= self.max_retries:
            return False
        if result.outcome != RuntimeOutcome.FAILED or result.error_code is None:
            return False
        if result.error_code not in self.retryable_error_codes:
            return False
        return result.resume_token is not None or self.allow_fresh_retry

    def delay_seconds(self, *, retry_number: int) -> float:
        """Return deterministic exponential backoff for a 1-based retry number."""

        if retry_number < 1:
            raise ValueError("retry_number must be positive")
        if self.base_delay_seconds == 0:
            return 0.0
        return min(
            self.base_delay_seconds * (2 ** (retry_number - 1)),
            self.max_delay_seconds,
        )


class ScriptRetryCondition(StrEnum):
    """Script-workflow failure conditions considered by automatic retry policy."""

    TEMPORARY_BUSY = "temporary_busy"
    EXPLICIT_RATE_LIMIT = "explicit_rate_limit"
    RECOVERABLE_NETWORK_FAILURE = "recoverable_network_failure"
    TEMPORARY_PAGE_READINESS_FAILURE = "temporary_page_readiness_failure"
    UNCERTAIN_EXTERNAL_EFFECT = "uncertain_external_effect"
    APPROVAL_DENIED = "approval_denied"
    PERMISSION_FAILURE = "permission_failure"
    SEMANTIC_LOCATOR_AMBIGUITY = "semantic_locator_ambiguity"
    DETERMINISTIC_VALIDATION_ERROR = "deterministic_validation_error"


_SAFE_SCRIPT_RETRY_CONDITIONS = frozenset(
    {
        ScriptRetryCondition.TEMPORARY_BUSY,
        ScriptRetryCondition.EXPLICIT_RATE_LIMIT,
        ScriptRetryCondition.RECOVERABLE_NETWORK_FAILURE,
        ScriptRetryCondition.TEMPORARY_PAGE_READINESS_FAILURE,
    }
)
_SCRIPT_RETRY_INTENT_VERSION = 1


class ScriptRetryDisposition(StrEnum):
    SCHEDULED = "scheduled"
    WAITING = "waiting"
    READY = "ready"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    NOT_RETRYABLE = "not_retryable"
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"
    BACKOFF_LIMIT_EXCEEDED = "backoff_limit_exceeded"
    DEADLINE_EXCEEDED = "deadline_exceeded"


def _as_utc(value: datetime, *, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _format_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_utc(value: object, *, field_name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a timezone-aware ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a timezone-aware ISO timestamp") from exc
    return _as_utc(parsed, field_name=field_name)


def _validate_retry_count(value: int, *, field_name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        qualifier = "positive" if minimum == 1 else "non-negative"
        raise ValueError(f"{field_name} must be a {qualifier} integer")
    return value


def _require_bool(value: bool, *, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{field_name} must be a boolean")
    return value


def _validate_retry_after(value: float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("retry_after_seconds must be a finite non-negative number")
    normalized = float(value)
    if not isfinite(normalized) or normalized < 0:
        raise ValueError("retry_after_seconds must be a finite non-negative number")
    return normalized


@dataclass(frozen=True, slots=True)
class ScriptRetryIntent:
    """JSON-safe durable intent for one already-authorized automatic retry."""

    operation_id: str
    condition: ScriptRetryCondition
    retry_number: int
    not_before_utc: datetime
    deadline_utc: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.operation_id, str) or not self.operation_id.strip():
            raise ValueError("operation_id must not be empty")
        if not isinstance(self.condition, ScriptRetryCondition):
            raise TypeError("condition must be a ScriptRetryCondition")
        _validate_retry_count(self.retry_number, field_name="retry_number", minimum=1)
        not_before = _as_utc(self.not_before_utc, field_name="not_before_utc")
        deadline = (
            None
            if self.deadline_utc is None
            else _as_utc(self.deadline_utc, field_name="deadline_utc")
        )
        if deadline is not None and not_before >= deadline:
            raise ValueError("not_before_utc must be earlier than deadline_utc")
        object.__setattr__(self, "not_before_utc", not_before)
        object.__setattr__(self, "deadline_utc", deadline)

    def to_payload(self) -> dict[str, object]:
        return {
            "version": _SCRIPT_RETRY_INTENT_VERSION,
            "operation_id": self.operation_id,
            "condition": self.condition.value,
            "retry_number": self.retry_number,
            "not_before_utc": _format_utc(self.not_before_utc),
            "deadline_utc": (
                None if self.deadline_utc is None else _format_utc(self.deadline_utc)
            ),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> ScriptRetryIntent:
        if not isinstance(payload, Mapping):
            raise TypeError("retry intent payload must be a mapping")
        expected_keys = {
            "version",
            "operation_id",
            "condition",
            "retry_number",
            "not_before_utc",
            "deadline_utc",
        }
        if set(payload) != expected_keys:
            raise ValueError("retry intent payload fields are invalid")
        version = payload["version"]
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version != _SCRIPT_RETRY_INTENT_VERSION
        ):
            raise ValueError("retry intent payload version is unsupported")
        operation_id = payload["operation_id"]
        if not isinstance(operation_id, str):
            raise TypeError("operation_id must be text")
        condition_value = payload["condition"]
        if not isinstance(condition_value, str):
            raise TypeError("condition must be text")
        try:
            condition = ScriptRetryCondition(condition_value)
        except ValueError as exc:
            raise ValueError("retry condition is unsupported") from exc
        retry_number = payload["retry_number"]
        if isinstance(retry_number, bool) or not isinstance(retry_number, int):
            raise TypeError("retry_number must be a positive integer")
        deadline_value = payload["deadline_utc"]
        deadline = (
            None
            if deadline_value is None
            else _parse_utc(deadline_value, field_name="deadline_utc")
        )
        return cls(
            operation_id=operation_id,
            condition=condition,
            retry_number=retry_number,
            not_before_utc=_parse_utc(
                payload["not_before_utc"],
                field_name="not_before_utc",
            ),
            deadline_utc=deadline,
        )


@dataclass(frozen=True, slots=True)
class ScriptRetryDecision:
    disposition: ScriptRetryDisposition
    condition: ScriptRetryCondition
    intent: ScriptRetryIntent | None = None


def plan_script_retry(
    policy: RetryPolicy,
    *,
    operation_id: str,
    condition: ScriptRetryCondition,
    retries_used: int,
    now: datetime,
    replay_safe: bool,
    deadline: datetime | None = None,
    retry_after_seconds: float | None = None,
    paused: bool = False,
    cancelled: bool = False,
) -> ScriptRetryDecision:
    """Plan one bounded retry without performing or persisting the external effect."""

    _validate_retry_count(retries_used, field_name="retries_used", minimum=0)
    if not isinstance(condition, ScriptRetryCondition):
        raise TypeError("condition must be a ScriptRetryCondition")
    _require_bool(replay_safe, field_name="replay_safe")
    _require_bool(paused, field_name="paused")
    _require_bool(cancelled, field_name="cancelled")
    current = _as_utc(now, field_name="now")
    deadline_utc = None if deadline is None else _as_utc(deadline, field_name="deadline")
    retry_after = _validate_retry_after(retry_after_seconds)
    if retry_after is not None and condition != ScriptRetryCondition.EXPLICIT_RATE_LIMIT:
        raise ValueError("retry_after_seconds is only valid for explicit rate limits")
    if cancelled:
        return ScriptRetryDecision(ScriptRetryDisposition.CANCELLED, condition)
    if condition not in _SAFE_SCRIPT_RETRY_CONDITIONS or not replay_safe:
        return ScriptRetryDecision(ScriptRetryDisposition.NOT_RETRYABLE, condition)
    if retries_used >= policy.max_retries:
        return ScriptRetryDecision(ScriptRetryDisposition.ATTEMPTS_EXHAUSTED, condition)
    if deadline_utc is not None and current >= deadline_utc:
        return ScriptRetryDecision(ScriptRetryDisposition.DEADLINE_EXCEEDED, condition)

    retry_number = retries_used + 1
    if not isfinite(float(policy.max_delay_seconds)):
        return ScriptRetryDecision(ScriptRetryDisposition.BACKOFF_LIMIT_EXCEEDED, condition)
    delay = policy.delay_seconds(retry_number=retry_number)
    if not isfinite(delay) or delay < 0 or delay > policy.max_delay_seconds:
        return ScriptRetryDecision(ScriptRetryDisposition.BACKOFF_LIMIT_EXCEEDED, condition)
    if retry_after is not None:
        if retry_after > policy.max_delay_seconds:
            return ScriptRetryDecision(
                ScriptRetryDisposition.BACKOFF_LIMIT_EXCEEDED,
                condition,
            )
        delay = max(delay, retry_after)
    not_before = current + timedelta(seconds=delay)
    if deadline_utc is not None and not_before >= deadline_utc:
        return ScriptRetryDecision(ScriptRetryDisposition.DEADLINE_EXCEEDED, condition)

    intent = ScriptRetryIntent(
        operation_id=operation_id,
        condition=condition,
        retry_number=retry_number,
        not_before_utc=not_before,
        deadline_utc=deadline_utc,
    )
    disposition = ScriptRetryDisposition.PAUSED if paused else ScriptRetryDisposition.SCHEDULED
    return ScriptRetryDecision(disposition, condition, intent)


def evaluate_script_retry_intent(
    intent: ScriptRetryIntent,
    policy: RetryPolicy,
    *,
    now: datetime,
    replay_safe: bool,
    paused: bool = False,
    cancelled: bool = False,
) -> ScriptRetryDecision:
    """Re-evaluate a durable retry intent after wait or process restart."""

    if not isinstance(intent, ScriptRetryIntent):
        raise TypeError("intent must be a ScriptRetryIntent")
    _require_bool(replay_safe, field_name="replay_safe")
    _require_bool(paused, field_name="paused")
    _require_bool(cancelled, field_name="cancelled")
    current = _as_utc(now, field_name="now")
    if cancelled:
        return ScriptRetryDecision(ScriptRetryDisposition.CANCELLED, intent.condition)
    if intent.condition not in _SAFE_SCRIPT_RETRY_CONDITIONS or not replay_safe:
        return ScriptRetryDecision(ScriptRetryDisposition.NOT_RETRYABLE, intent.condition)
    if intent.retry_number > policy.max_retries:
        return ScriptRetryDecision(
            ScriptRetryDisposition.ATTEMPTS_EXHAUSTED,
            intent.condition,
        )
    if intent.deadline_utc is not None and current >= intent.deadline_utc:
        return ScriptRetryDecision(ScriptRetryDisposition.DEADLINE_EXCEEDED, intent.condition)
    if paused:
        return ScriptRetryDecision(ScriptRetryDisposition.PAUSED, intent.condition, intent)
    if current < intent.not_before_utc:
        return ScriptRetryDecision(ScriptRetryDisposition.WAITING, intent.condition, intent)
    return ScriptRetryDecision(ScriptRetryDisposition.READY, intent.condition, intent)
