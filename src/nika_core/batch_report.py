from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr

from nika_core.runtime.idempotency import IdempotencyRecord, IdempotencyStatus

_MAX_TEXT = 240
_MAX_REFERENCE = 512
_HTML_TAG_RE = re.compile(r"<\s*/?\s*[a-zA-Z!][^>]*>")
_SENSITIVE_RE = re.compile(
    r"(?i)(?:authorization|proxy-authorization|set-cookie|cookie|bearer|"
    r"api[_-]?key|token|password|passwd|secret|credential|signature|headers?|"
    r"session[_-]?id)"
)
_SAFE_OPAQUE_REF_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,239}\Z")
_ALLOWED_REF_PREFIXES = ("artifact:", "evidence:", "record:", "result:", "sha256:")
_BATCH_EFFECT_OPERATION_TYPE = "v01.batch_target_effect"


class BatchReportProjectionError(RuntimeError):
    """Canonical report inputs are malformed, missing authority, or contradict each other."""


class TargetReportStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"


class TargetReportFacts(BaseModel):
    """Read-only facts supplied by canonical owners that are outside the batch cursor itself."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_id: StrictStr
    input_order: StrictInt = Field(ge=0)
    display_name: StrictStr | None = None
    opened: StrictBool | None = None
    attempted: StrictBool | None = None
    terminal_status: TargetReportStatus | None = None
    reason_code: StrictStr | None = None
    evidence_ref: StrictStr | None = None
    updated_at: StrictStr | None = None
    next_retry_at: StrictStr | None = None
    next_wake_at: StrictStr | None = None


class TargetReportItem(BaseModel):
    """Bounded secret-minimized view of one declared target input position."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_identity: StrictStr
    input_order: StrictInt = Field(ge=0)
    display_name: StrictStr
    status: TargetReportStatus
    attempt_count: StrictInt = Field(ge=0)
    opened: StrictBool | None
    attempted: StrictBool
    reason: StrictStr | None
    evidence_ref: StrictStr | None
    updated_at: StrictStr
    next_retry_at: StrictStr | None = None
    next_wake_at: StrictStr | None = None


class TargetCursorView(Protocol):
    target_id: str
    input_positions: Sequence[int]
    input_fingerprint: str
    operation_key: str
    attempt_state: Any
    attempts: int
    confirmed_result: Mapping[str, Any] | None
    uncertain_result: Mapping[str, Any] | None


class ScheduledIntentView(Protocol):
    target_id: str
    not_before: str | None


class BatchCursorStateView(Protocol):
    task_id: str
    cursor_id: str
    input_count: int
    targets: Sequence[TargetCursorView]
    next_scheduled_intent: ScheduledIntentView | None


def project_batch_report(
    state: BatchCursorStateView,
    *,
    batch_updated_at: datetime | str,
    effect_records: Mapping[str, IdempotencyRecord] | None = None,
    facts: Sequence[TargetReportFacts] = (),
) -> tuple[TargetReportItem, ...]:
    """Project canonical durable state without mutating or persisting any authority."""
    task_id = _required_identity("task_id", state.task_id)
    cursor_id = _required_identity("cursor_id", state.cursor_id)
    batch_timestamp = _parse_timestamp(batch_updated_at)
    records = effect_records or {}
    fact_map = _index_facts(facts)

    rows: list[TargetReportItem] = []
    declared_positions: set[int] = set()
    target_keys: set[tuple[str, int]] = set()

    for target in state.targets:
        target_id = _required_identity("target_id", target.target_id)
        operation_key = _required_identity("operation_key", target.operation_key)
        input_fingerprint = _required_identity("input_fingerprint", target.input_fingerprint)
        attempts = _non_negative("attempts", target.attempts)
        positions = _positions(target.input_positions)
        record = records.get(operation_key)
        _validate_effect_record(
            record,
            task_id=task_id,
            operation_key=operation_key,
            input_fingerprint=input_fingerprint,
        )
        cursor_status = _attempt_state(target.attempt_state)
        _require_effect_authority(cursor_status, record)

        for input_order in positions:
            key = (target_id, input_order)
            if key in target_keys or input_order in declared_positions:
                raise BatchReportProjectionError("declared target input positions overlap")
            target_keys.add(key)
            declared_positions.add(input_order)
            fact = fact_map.pop(key, None)
            status = _project_status(cursor_status, record, fact)
            attempted = _project_attempted(attempts, record, fact)
            opened = _project_opened(attempted, fact)
            reason = _project_reason(status, target, fact)
            evidence_ref = _project_evidence_ref(target, record, fact)
            next_retry_at, next_wake_at = _project_next_times(state, target_id, status, fact)
            updated_at = _latest_timestamp(
                batch_timestamp,
                record.updated_at if record is not None else None,
                fact.updated_at if fact is not None else None,
            )
            rows.append(
                TargetReportItem(
                    target_identity=_stable_target_identity(
                        task_id=task_id,
                        cursor_id=cursor_id,
                        target_id=target_id,
                        input_order=input_order,
                    ),
                    input_order=input_order,
                    display_name=_display_name(fact, input_order),
                    status=status,
                    attempt_count=attempts,
                    opened=opened,
                    attempted=attempted,
                    reason=reason,
                    evidence_ref=evidence_ref,
                    updated_at=updated_at,
                    next_retry_at=next_retry_at,
                    next_wake_at=next_wake_at,
                )
            )

    if fact_map:
        raise BatchReportProjectionError("report facts reference undeclared target input")
    input_count = _non_negative("input_count", state.input_count)
    if declared_positions != set(range(input_count)):
        raise BatchReportProjectionError("declared report positions do not match input_count")
    rows.sort(key=lambda item: item.input_order)
    return tuple(rows)


def sanitize_report_reference(value: str | None) -> str | None:
    """Return a bounded reference with credential-bearing URL parts removed."""
    if value is None:
        return None
    text = value.strip()
    if not text or len(text) > _MAX_REFERENCE or _HTML_TAG_RE.search(text):
        return None
    if text.lower().startswith(("http://", "https://")):
        try:
            parsed = urlsplit(text)
            if not parsed.hostname or parsed.scheme.lower() not in {"http", "https"}:
                return None
            port = parsed.port
        except ValueError:
            return None
        host = parsed.hostname
        if host is None:
            return None
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        netloc = f"{host}:{port}" if port is not None else host
        path = parsed.path
        decoded_path = unquote(path)
        if _SENSITIVE_RE.search(decoded_path) or any(
            len(segment) > 80 for segment in decoded_path.split("/")
        ):
            path = ""
        safe = urlunsplit((parsed.scheme.lower(), netloc, path, "", ""))
        return safe if len(safe) <= _MAX_TEXT else None
    if "://" in text or any(marker in text for marker in ("?", "@", "=")):
        return None
    if (
        _SENSITIVE_RE.search(text)
        or not text.lower().startswith(_ALLOWED_REF_PREFIXES)
        or not _SAFE_OPAQUE_REF_RE.fullmatch(text)
    ):
        return None
    return text


def _index_facts(facts: Sequence[TargetReportFacts]) -> dict[tuple[str, int], TargetReportFacts]:
    result: dict[tuple[str, int], TargetReportFacts] = {}
    for fact in facts:
        target_id = _required_identity("fact target_id", fact.target_id)
        key = (target_id, int(fact.input_order))
        if key in result:
            raise BatchReportProjectionError("duplicate report facts for target input")
        if fact.terminal_status is not None and fact.terminal_status not in {
            TargetReportStatus.FAILED,
            TargetReportStatus.SKIPPED,
            TargetReportStatus.CANCELLED,
        }:
            raise BatchReportProjectionError(
                "supplemental facts may only declare known terminal non-effect status"
            )
        if fact.terminal_status is not None and (
            fact.next_retry_at is not None or fact.next_wake_at is not None
        ):
            raise BatchReportProjectionError("terminal report facts cannot schedule future work")
        if fact.updated_at is not None:
            _parse_timestamp(fact.updated_at)
        if fact.next_retry_at is not None:
            _parse_timestamp(fact.next_retry_at)
        if fact.next_wake_at is not None:
            _parse_timestamp(fact.next_wake_at)
        result[key] = fact
    return result


def _project_status(
    cursor_status: str,
    record: IdempotencyRecord | None,
    fact: TargetReportFacts | None,
) -> TargetReportStatus:
    if record is not None:
        if record.status is IdempotencyStatus.UNCERTAIN:
            return TargetReportStatus.UNCERTAIN
        if record.status is IdempotencyStatus.COMPLETED:
            return TargetReportStatus.SUCCEEDED
        if record.status is IdempotencyStatus.PENDING:
            return TargetReportStatus.RUNNING

    if cursor_status == "UNCERTAIN":
        return TargetReportStatus.UNCERTAIN
    if cursor_status in {"CONFIRMED", "SUCCEEDED"}:
        return TargetReportStatus.SUCCEEDED
    if cursor_status in {"IN_FLIGHT", "RUNNING"}:
        return TargetReportStatus.RUNNING
    if cursor_status == "FAILED":
        return TargetReportStatus.FAILED
    if cursor_status == "SKIPPED":
        return TargetReportStatus.SKIPPED
    if cursor_status in {"CANCELLED", "CANCELED"}:
        return TargetReportStatus.CANCELLED
    if cursor_status not in {"PENDING", "PREPARED"}:
        raise BatchReportProjectionError("unknown canonical target attempt state")
    if fact is not None and fact.terminal_status is not None:
        return fact.terminal_status
    return TargetReportStatus.PENDING


def _project_attempted(
    attempts: int,
    record: IdempotencyRecord | None,
    fact: TargetReportFacts | None,
) -> bool:
    canonical = attempts > 0 or record is not None
    if fact is None or fact.attempted is None:
        return canonical
    if canonical and fact.attempted is False:
        raise BatchReportProjectionError(
            "supplemental attempted=false contradicts durable effect state"
        )
    return canonical or bool(fact.attempted)


def _project_opened(attempted: bool, fact: TargetReportFacts | None) -> bool | None:
    if fact is not None and fact.opened is not None:
        return bool(fact.opened)
    return False if not attempted else None


def _project_reason(
    status: TargetReportStatus,
    target: TargetCursorView,
    fact: TargetReportFacts | None,
) -> str | None:
    candidates: list[Any] = []
    if fact is not None:
        candidates.append(fact.reason_code)
    if status is TargetReportStatus.UNCERTAIN:
        candidates.extend(_mapping_values(target.uncertain_result, "reason_code", "reason"))
    elif status is TargetReportStatus.SUCCEEDED:
        candidates.extend(_mapping_values(target.confirmed_result, "reason_code", "reason"))
    for candidate in candidates:
        safe = _sanitize_text(candidate)
        if safe is not None:
            return safe
    defaults = {
        TargetReportStatus.SUCCEEDED: "verified_success",
        TargetReportStatus.FAILED: "terminal_failure",
        TargetReportStatus.UNCERTAIN: "reconciliation_required",
        TargetReportStatus.SKIPPED: "skipped",
        TargetReportStatus.CANCELLED: "cancelled",
    }
    return defaults.get(status)


def _project_evidence_ref(
    target: TargetCursorView,
    record: IdempotencyRecord | None,
    fact: TargetReportFacts | None,
) -> str | None:
    sources: list[Mapping[str, Any] | None] = []
    if record is not None and isinstance(record.result, Mapping):
        sources.append(record.result)
    sources.extend((target.confirmed_result, target.uncertain_result))
    for source in sources:
        for value in _mapping_values(source, "safe_evidence_ref", "evidence_ref", "result_ref"):
            if isinstance(value, str):
                safe = sanitize_report_reference(value)
                if safe is not None:
                    return safe
    if fact is not None:
        return sanitize_report_reference(fact.evidence_ref)
    return None


def _project_next_times(
    state: BatchCursorStateView,
    target_id: str,
    status: TargetReportStatus,
    fact: TargetReportFacts | None,
) -> tuple[str | None, str | None]:
    if status in {
        TargetReportStatus.SUCCEEDED,
        TargetReportStatus.FAILED,
        TargetReportStatus.UNCERTAIN,
        TargetReportStatus.SKIPPED,
        TargetReportStatus.CANCELLED,
    }:
        return None, None
    retry = _canonical_timestamp(fact.next_retry_at) if fact and fact.next_retry_at else None
    wake = _canonical_timestamp(fact.next_wake_at) if fact and fact.next_wake_at else None
    intent = state.next_scheduled_intent
    if intent is not None and intent.target_id == target_id and intent.not_before is not None:
        intent_wake = _canonical_timestamp(intent.not_before)
        if wake is not None and wake != intent_wake:
            raise BatchReportProjectionError(
                "supplemental wake time contradicts durable batch intent"
            )
        wake = intent_wake
    return retry, wake


def _display_name(fact: TargetReportFacts | None, input_order: int) -> str:
    safe = _sanitize_text(fact.display_name) if fact is not None else None
    return safe or f"Target {input_order + 1}"


def _validate_effect_record(
    record: IdempotencyRecord | None,
    *,
    task_id: str,
    operation_key: str,
    input_fingerprint: str,
) -> None:
    if record is None:
        return
    if record.operation_key != operation_key:
        raise BatchReportProjectionError("effect evidence operation key mismatch")
    if record.task_id != task_id:
        raise BatchReportProjectionError("effect evidence task identity mismatch")
    if record.operation_type != _BATCH_EFFECT_OPERATION_TYPE:
        raise BatchReportProjectionError("effect evidence operation type mismatch")
    if record.input_fingerprint != input_fingerprint:
        raise BatchReportProjectionError("effect evidence input fingerprint mismatch")
    _parse_timestamp(record.created_at)
    _parse_timestamp(record.updated_at)


def _require_effect_authority(cursor_status: str, record: IdempotencyRecord | None) -> None:
    if cursor_status in {"IN_FLIGHT", "RUNNING", "CONFIRMED", "SUCCEEDED", "UNCERTAIN"}:
        if record is None:
            raise BatchReportProjectionError("effect-bearing target lacks durable effect evidence")


def _attempt_state(value: Any) -> str:
    raw = getattr(value, "value", value)
    if not isinstance(raw, str) or not raw.strip():
        raise BatchReportProjectionError("target attempt_state is malformed")
    return raw.strip().upper()


def _positions(values: Sequence[int]) -> tuple[int, ...]:
    positions: list[int] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise BatchReportProjectionError("target input_positions are malformed")
        positions.append(value)
    if not positions or positions != sorted(set(positions)):
        raise BatchReportProjectionError("target input_positions are malformed")
    return tuple(positions)


def _non_negative(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BatchReportProjectionError(f"{name} must be a non-negative integer")
    return value


def _required_identity(name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BatchReportProjectionError(f"{name} must not be empty")
    return value.strip()


def _stable_target_identity(
    *, task_id: str, cursor_id: str, target_id: str, input_order: int
) -> str:
    body = "\0".join((task_id, cursor_id, target_id, str(input_order))).encode("utf-8")
    return f"target:{hashlib.sha256(body).hexdigest()[:24]}"


def _sanitize_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text or len(text) > _MAX_TEXT:
        return None
    if _HTML_TAG_RE.search(text) or _SENSITIVE_RE.search(text):
        return None
    if "http://" in text.lower() or "https://" in text.lower():
        return None
    return text


def _mapping_values(source: Mapping[str, Any] | None, *keys: str) -> list[Any]:
    if not isinstance(source, Mapping):
        return []
    return [source[key] for key in keys if key in source]


def _latest_timestamp(base: datetime, *values: str | None) -> str:
    latest = base
    for value in values:
        if value is None:
            continue
        parsed = _parse_timestamp(value)
        if parsed > latest:
            latest = parsed
    return latest.isoformat()


def _canonical_timestamp(value: str) -> str:
    return _parse_timestamp(value).isoformat()


def _parse_timestamp(value: datetime | str) -> datetime:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise BatchReportProjectionError("report timestamp is malformed") from exc
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise BatchReportProjectionError("report timestamp has invalid type")
    if parsed.tzinfo is None:
        raise BatchReportProjectionError("report timestamp must be timezone-aware")
    return parsed.astimezone(UTC)
