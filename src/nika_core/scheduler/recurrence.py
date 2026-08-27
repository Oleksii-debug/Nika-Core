from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from nika_core.scheduler.contracts import ScheduledJob, SchedulerPort, TriggerKind
from nika_core.scheduler.store import ScheduledJobStore

_RECURRENCE_PAYLOAD_KEY = "_nika_recurrence_v1"
_TARGET_PAYLOAD_KEY = "target_payload"
_RECURRENCE_VERSION = 1


class MissedRunPolicy(StrEnum):
    """V0.1 missed-run policy.

    One overdue intent is allowed to run after restart/resume. After that run completes,
    intermediate missed slots are skipped and exactly one future intent is persisted.
    """

    COALESCE_ONE = "coalesce_one"


class RecurrenceStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class RecurrenceDecision(StrEnum):
    CONTINUE = "continue"
    STOP = "stop"


class RecurrenceTerminalReason(StrEnum):
    CONDITION_MET = "condition_met"
    DEADLINE = "deadline"


@dataclass(frozen=True, slots=True)
class RecurrenceInvocation:
    recurrence_id: str
    occurrence_id: str
    scheduled_for: datetime
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RecurrenceState:
    recurrence_id: str
    action_id: str
    interval_seconds: int
    anchor_at: datetime
    deadline_at: datetime | None
    status: RecurrenceStatus
    missed_run_policy: MissedRunPolicy
    next_due_at: datetime | None
    next_occurrence_id: str | None
    last_completed_due_at: datetime | None
    last_completed_occurrence_id: str | None
    terminal_reason: RecurrenceTerminalReason | None


OccurrenceHandler = Callable[[RecurrenceInvocation], RecurrenceDecision | None]
OccurrenceHandlerResolver = Callable[[str], OccurrenceHandler]
Clock = Callable[[], datetime]


class DurableRecurrenceService:
    """Thin durable recurrence policy over SchedulerPort and ScheduledJobStore.

    APScheduler remains the timing engine. This service persists one date-trigger intent at a
    time so restart/resume can reconstruct exactly one next occurrence and cannot create a
    catch-up storm.
    """

    ACTION_ID = "scheduler.recurrence.dispatch"

    def __init__(
        self,
        *,
        jobs: ScheduledJobStore,
        scheduler: SchedulerPort,
        handler_resolver: OccurrenceHandlerResolver,
        clock: Clock | None = None,
    ) -> None:
        self._jobs = jobs
        self._scheduler = scheduler
        self._handler_resolver = handler_resolver
        self._clock = clock or _utc_now

    def create(
        self,
        *,
        recurrence_id: str,
        action_id: str,
        interval_seconds: int,
        start_at: datetime,
        payload: dict[str, Any] | None = None,
        deadline_at: datetime | None = None,
    ) -> RecurrenceState:
        recurrence_key = _required_text(recurrence_id, "recurrence_id")
        target_action = _required_text(action_id, "action_id")
        interval = _validate_interval(interval_seconds)
        anchor = _require_aware_utc(start_at, "start_at")
        deadline = (
            _require_aware_utc(deadline_at, "deadline_at") if deadline_at is not None else None
        )
        if deadline is not None and deadline <= anchor:
            status = RecurrenceStatus.COMPLETED
            terminal_reason = RecurrenceTerminalReason.DEADLINE
            next_due = None
        elif deadline is not None and self._now() >= deadline:
            status = RecurrenceStatus.COMPLETED
            terminal_reason = RecurrenceTerminalReason.DEADLINE
            next_due = None
        else:
            status = RecurrenceStatus.ACTIVE
            terminal_reason = None
            next_due = anchor

        user_payload = dict(payload or {})
        job_id = _job_id(recurrence_key)
        existing = self._jobs.get(job_id)
        if existing is not None:
            state, existing_payload = _decode_job(existing, expected_recurrence_id=recurrence_key)
            expected = (
                target_action,
                interval,
                anchor,
                deadline,
                user_payload,
            )
            actual = (
                state.action_id,
                state.interval_seconds,
                state.anchor_at,
                state.deadline_at,
                existing_payload,
            )
            if actual != expected:
                raise ValueError("recurrence_id is already bound to a different recurrence")
            return state

        state = RecurrenceState(
            recurrence_id=recurrence_key,
            action_id=target_action,
            interval_seconds=interval,
            anchor_at=anchor,
            deadline_at=deadline,
            status=status,
            missed_run_policy=MissedRunPolicy.COALESCE_ONE,
            next_due_at=next_due,
            next_occurrence_id=(
                _occurrence_id(recurrence_key, next_due) if next_due is not None else None
            ),
            last_completed_due_at=None,
            last_completed_occurrence_id=None,
            terminal_reason=terminal_reason,
        )
        self._persist(state, user_payload)
        return state

    def get(self, recurrence_id: str) -> RecurrenceState | None:
        recurrence_key = _required_text(recurrence_id, "recurrence_id")
        job = self._jobs.get(_job_id(recurrence_key))
        if job is None:
            return None
        state, _ = _decode_job(job, expected_recurrence_id=recurrence_key)
        return state

    def pause(self, recurrence_id: str) -> RecurrenceState:
        state, payload = self._required(recurrence_id)
        if state.status is not RecurrenceStatus.ACTIVE:
            return state
        paused = replace(state, status=RecurrenceStatus.PAUSED)
        self._persist(paused, payload)
        return paused

    def resume(self, recurrence_id: str) -> RecurrenceState:
        state, payload = self._required(recurrence_id)
        if state.status is not RecurrenceStatus.PAUSED:
            return state
        now = self._now()
        if state.deadline_at is not None and now >= state.deadline_at:
            completed = replace(
                state,
                status=RecurrenceStatus.COMPLETED,
                next_due_at=None,
                next_occurrence_id=None,
                terminal_reason=RecurrenceTerminalReason.DEADLINE,
            )
            self._persist(completed, payload)
            return completed
        if state.next_due_at is None:
            raise ValueError("paused recurrence is missing its next durable intent")
        resumed = replace(state, status=RecurrenceStatus.ACTIVE)
        self._persist(resumed, payload)
        return resumed

    def cancel(self, recurrence_id: str) -> RecurrenceState:
        state, payload = self._required(recurrence_id)
        if state.status in {RecurrenceStatus.CANCELLED, RecurrenceStatus.COMPLETED}:
            return state
        cancelled = replace(
            state,
            status=RecurrenceStatus.CANCELLED,
            next_due_at=None,
            next_occurrence_id=None,
            terminal_reason=None,
        )
        self._persist(cancelled, payload)
        return cancelled

    def action_handler(self, payload: dict[str, Any]) -> None:
        recurrence_id = _required_text(payload.get("recurrence_id"), "recurrence_id")
        state, target_payload = self._required(recurrence_id)
        if state.status is not RecurrenceStatus.ACTIVE:
            return
        if state.next_due_at is None or state.next_occurrence_id is None:
            raise ValueError("active recurrence is missing its next durable intent")
        expected_id = _occurrence_id(state.recurrence_id, state.next_due_at)
        if state.next_occurrence_id != expected_id:
            raise ValueError("recurrence occurrence identity is corrupt")
        if state.last_completed_occurrence_id == state.next_occurrence_id:
            raise ValueError("completed occurrence cannot remain the next durable intent")

        now = self._now()
        if state.deadline_at is not None and now >= state.deadline_at:
            self._complete_terminal(
                state,
                target_payload,
                reason=RecurrenceTerminalReason.DEADLINE,
            )
            return
        if now < state.next_due_at:
            return

        invocation = RecurrenceInvocation(
            recurrence_id=state.recurrence_id,
            occurrence_id=state.next_occurrence_id,
            scheduled_for=state.next_due_at,
            payload=dict(target_payload),
        )
        decision = self._handler_resolver(state.action_id)(invocation)
        if decision is None or decision is RecurrenceDecision.CONTINUE:
            stop = False
        elif decision is RecurrenceDecision.STOP:
            stop = True
        else:
            raise ValueError("recurrence handler returned an unsupported decision")
        self._finish_occurrence(
            state.recurrence_id,
            invocation,
            stop=stop,
        )

    def _finish_occurrence(
        self,
        recurrence_id: str,
        invocation: RecurrenceInvocation,
        *,
        stop: bool,
    ) -> RecurrenceState:
        current, payload = self._required(recurrence_id)
        if current.status is RecurrenceStatus.CANCELLED:
            return current
        if current.last_completed_occurrence_id == invocation.occurrence_id:
            return current
        if (
            current.next_occurrence_id != invocation.occurrence_id
            or current.next_due_at != invocation.scheduled_for
        ):
            raise ValueError("stale recurrence occurrence cannot advance durable state")

        now = self._now()
        if stop:
            completed = replace(
                current,
                status=RecurrenceStatus.COMPLETED,
                next_due_at=None,
                next_occurrence_id=None,
                last_completed_due_at=invocation.scheduled_for,
                last_completed_occurrence_id=invocation.occurrence_id,
                terminal_reason=RecurrenceTerminalReason.CONDITION_MET,
            )
            self._persist(completed, payload)
            return completed

        next_due = _first_future_slot(
            invocation.scheduled_for,
            interval_seconds=current.interval_seconds,
            now=now,
        )
        if current.deadline_at is not None and next_due >= current.deadline_at:
            completed = replace(
                current,
                status=RecurrenceStatus.COMPLETED,
                next_due_at=None,
                next_occurrence_id=None,
                last_completed_due_at=invocation.scheduled_for,
                last_completed_occurrence_id=invocation.occurrence_id,
                terminal_reason=RecurrenceTerminalReason.DEADLINE,
            )
            self._persist(completed, payload)
            return completed

        next_state = replace(
            current,
            next_due_at=next_due,
            next_occurrence_id=_occurrence_id(current.recurrence_id, next_due),
            last_completed_due_at=invocation.scheduled_for,
            last_completed_occurrence_id=invocation.occurrence_id,
        )
        self._persist(next_state, payload)
        return next_state

    def _complete_terminal(
        self,
        state: RecurrenceState,
        payload: dict[str, Any],
        *,
        reason: RecurrenceTerminalReason,
    ) -> RecurrenceState:
        completed = replace(
            state,
            status=RecurrenceStatus.COMPLETED,
            next_due_at=None,
            next_occurrence_id=None,
            terminal_reason=reason,
        )
        self._persist(completed, payload)
        return completed

    def _required(self, recurrence_id: str) -> tuple[RecurrenceState, dict[str, Any]]:
        recurrence_key = _required_text(recurrence_id, "recurrence_id")
        job = self._jobs.get(_job_id(recurrence_key))
        if job is None:
            raise KeyError(f"unknown recurrence: {recurrence_key}")
        return _decode_job(job, expected_recurrence_id=recurrence_key)

    def _persist(self, state: RecurrenceState, target_payload: dict[str, Any]) -> None:
        enabled = state.status is RecurrenceStatus.ACTIVE and state.next_due_at is not None
        run_date = state.next_due_at or state.last_completed_due_at or state.anchor_at
        job = ScheduledJob(
            job_id=_job_id(state.recurrence_id),
            action_id=self.ACTION_ID,
            trigger_kind=TriggerKind.DATE,
            trigger={"run_date": _iso(run_date)},
            payload={
                "recurrence_id": state.recurrence_id,
                _RECURRENCE_PAYLOAD_KEY: _encode_state(state),
                _TARGET_PAYLOAD_KEY: dict(target_payload),
            },
            enabled=enabled,
            coalesce=True,
            max_instances=1,
            misfire_grace_seconds=None,
        )
        self._scheduler.upsert(job)

    def _now(self) -> datetime:
        return _require_aware_utc(self._clock(), "clock value")


def _encode_state(state: RecurrenceState) -> dict[str, Any]:
    return {
        "version": _RECURRENCE_VERSION,
        "recurrence_id": state.recurrence_id,
        "action_id": state.action_id,
        "interval_seconds": state.interval_seconds,
        "anchor_at": _iso(state.anchor_at),
        "deadline_at": _iso(state.deadline_at) if state.deadline_at is not None else None,
        "status": state.status.value,
        "missed_run_policy": state.missed_run_policy.value,
        "next_due_at": _iso(state.next_due_at) if state.next_due_at is not None else None,
        "next_occurrence_id": state.next_occurrence_id,
        "last_completed_due_at": (
            _iso(state.last_completed_due_at) if state.last_completed_due_at is not None else None
        ),
        "last_completed_occurrence_id": state.last_completed_occurrence_id,
        "terminal_reason": (
            state.terminal_reason.value if state.terminal_reason is not None else None
        ),
    }


def _decode_job(
    job: ScheduledJob,
    *,
    expected_recurrence_id: str,
) -> tuple[RecurrenceState, dict[str, Any]]:
    if job.action_id != DurableRecurrenceService.ACTION_ID:
        raise ValueError("durable recurrence job has an unexpected action_id")
    metadata = job.payload.get(_RECURRENCE_PAYLOAD_KEY)
    target_payload = job.payload.get(_TARGET_PAYLOAD_KEY)
    if not isinstance(metadata, dict) or not isinstance(target_payload, dict):
        raise ValueError("durable recurrence payload is corrupt")
    if metadata.get("version") != _RECURRENCE_VERSION:
        raise ValueError("unsupported durable recurrence payload version")
    recurrence_id = _required_text(metadata.get("recurrence_id"), "persisted recurrence_id")
    if recurrence_id != expected_recurrence_id:
        raise ValueError("durable recurrence identity mismatch")
    interval = _validate_interval(metadata.get("interval_seconds"))
    anchor = _parse_iso(metadata.get("anchor_at"), "anchor_at")
    deadline = _parse_optional_iso(metadata.get("deadline_at"), "deadline_at")
    next_due = _parse_optional_iso(metadata.get("next_due_at"), "next_due_at")
    last_due = _parse_optional_iso(metadata.get("last_completed_due_at"), "last_completed_due_at")
    try:
        status = RecurrenceStatus(metadata.get("status"))
        policy = MissedRunPolicy(metadata.get("missed_run_policy"))
    except (TypeError, ValueError) as exc:
        raise ValueError("durable recurrence enum state is corrupt") from exc
    terminal_raw = metadata.get("terminal_reason")
    try:
        terminal_reason = (
            RecurrenceTerminalReason(terminal_raw) if terminal_raw is not None else None
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("durable recurrence terminal reason is corrupt") from exc
    next_id = metadata.get("next_occurrence_id")
    last_id = metadata.get("last_completed_occurrence_id")
    if next_id is not None and (not isinstance(next_id, str) or not next_id.strip()):
        raise ValueError("durable recurrence next occurrence identity is corrupt")
    if last_id is not None and (not isinstance(last_id, str) or not last_id.strip()):
        raise ValueError("durable recurrence completion identity is corrupt")
    if (next_due is None) != (next_id is None):
        raise ValueError("durable recurrence next intent is incomplete")
    if (last_due is None) != (last_id is None):
        raise ValueError("durable recurrence completion cursor is incomplete")
    if next_due is not None and next_id != _occurrence_id(recurrence_id, next_due):
        raise ValueError("durable recurrence next occurrence identity is corrupt")
    if last_due is not None and last_id != _occurrence_id(recurrence_id, last_due):
        raise ValueError("durable recurrence completion identity is corrupt")
    if status in {RecurrenceStatus.ACTIVE, RecurrenceStatus.PAUSED} and next_due is None:
        raise ValueError("non-terminal recurrence is missing its next durable intent")
    if status in {RecurrenceStatus.CANCELLED, RecurrenceStatus.COMPLETED} and next_due is not None:
        raise ValueError("terminal recurrence cannot retain a next durable intent")
    if bool(job.enabled) != (status is RecurrenceStatus.ACTIVE and next_due is not None):
        raise ValueError("durable recurrence enabled state does not match lifecycle state")
    return (
        RecurrenceState(
            recurrence_id=recurrence_id,
            action_id=_required_text(metadata.get("action_id"), "persisted action_id"),
            interval_seconds=interval,
            anchor_at=anchor,
            deadline_at=deadline,
            status=status,
            missed_run_policy=policy,
            next_due_at=next_due,
            next_occurrence_id=next_id,
            last_completed_due_at=last_due,
            last_completed_occurrence_id=last_id,
            terminal_reason=terminal_reason,
        ),
        dict(target_payload),
    )


def _first_future_slot(
    scheduled_for: datetime,
    *,
    interval_seconds: int,
    now: datetime,
) -> datetime:
    interval = timedelta(seconds=interval_seconds)
    candidate = scheduled_for + interval
    if candidate > now:
        return candidate
    skipped = (now - candidate) // interval + 1
    return candidate + skipped * interval


def _occurrence_id(recurrence_id: str, scheduled_for: datetime) -> str:
    material = f"{recurrence_id}\0{_iso(scheduled_for)}".encode("utf-8")
    return "recurrence-occurrence-v1:" + hashlib.sha256(material).hexdigest()


def _job_id(recurrence_id: str) -> str:
    digest = hashlib.sha256(recurrence_id.encode("utf-8")).hexdigest()
    return f"recurrence-v1:{digest}"


def _validate_interval(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("interval_seconds must be a positive integer")
    return value


def _parse_iso(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a timezone-aware ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be a valid ISO-8601 datetime") from exc
    return _require_aware_utc(parsed, label)


def _parse_optional_iso(value: object, label: str) -> datetime | None:
    if value is None:
        return None
    return _parse_iso(value, label)


def _require_aware_utc(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} is required")
    return value.strip()


def _utc_now() -> datetime:
    return datetime.now(UTC)
