from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, cast

from nika_core.kernel.task_state import TaskState
from nika_core.scheduler.contracts import ScheduledJob, SchedulerPort, TriggerKind
from nika_core.scheduler.store import IMMUTABLE_JOB_BINDING_KEY, ScheduledJobStore

_RECURRENCE_PAYLOAD_KEY = "_nika_recurrence_v1"
_TARGET_PAYLOAD_KEY = "target_payload"
_TASK_ID_KEY = "task_id"
_RECURRENCE_VERSION = 2
_MAX_PAYLOAD_DEPTH = 32
_MAX_PAYLOAD_JSON_BYTES = 262_144
_IRREVERSIBLY_TERMINAL_TASK_STATES = frozenset(
    {TaskState.CANCELLED, TaskState.COMPLETED, TaskState.ARCHIVED}
)


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
    RANGE_EXHAUSTED = "range_exhausted"


@dataclass(frozen=True, slots=True)
class RecurrenceInvocation:
    recurrence_id: str
    occurrence_id: str
    scheduled_for: datetime
    payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RecurrenceState:
    recurrence_id: str
    task_id: str
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
        self._clock = _utc_now if clock is None else clock

    def create(
        self,
        *,
        recurrence_id: str,
        task_id: str,
        action_id: str,
        interval_seconds: int,
        start_at: datetime,
        payload: dict[str, Any] | None = None,
        deadline_at: datetime | None = None,
    ) -> RecurrenceState:
        recurrence_key = _required_text(recurrence_id, "recurrence_id")
        task_key = _canonical_task_id(task_id)
        target_action = _required_text(action_id, "action_id")
        interval = _validate_interval(interval_seconds)
        anchor = _require_aware_utc(start_at, "start_at")
        _validate_interval_origin(anchor, interval, "start_at")
        deadline = (
            _require_aware_utc(deadline_at, "deadline_at") if deadline_at is not None else None
        )
        if deadline is not None and (deadline <= anchor or self._now() >= deadline):
            status = RecurrenceStatus.COMPLETED
            terminal_reason = RecurrenceTerminalReason.DEADLINE
            next_due = None
        else:
            status = RecurrenceStatus.ACTIVE
            terminal_reason = None
            next_due = anchor

        user_payload = _canonical_payload(payload)
        job_id = _job_id(recurrence_key)
        existing = self._jobs.get(job_id)
        if existing is not None:
            state, existing_payload = self._decode_with_task_authority(
                existing,
                expected_recurrence_id=recurrence_key,
            )
            requested_binding = _definition_fingerprint(
                recurrence_id=recurrence_key,
                task_id=task_key,
                action_id=target_action,
                interval_seconds=interval,
                anchor_at=anchor,
                deadline_at=deadline,
                target_payload=user_payload,
            )
            existing_binding = _definition_fingerprint(
                recurrence_id=state.recurrence_id,
                task_id=state.task_id,
                action_id=state.action_id,
                interval_seconds=state.interval_seconds,
                anchor_at=state.anchor_at,
                deadline_at=state.deadline_at,
                target_payload=existing_payload,
            )
            if existing_binding != requested_binding:
                raise ValueError("recurrence_id is already bound to a different recurrence")
            return state

        state = RecurrenceState(
            recurrence_id=recurrence_key,
            task_id=task_key,
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
        state, _ = self._decode_with_task_authority(
            job,
            expected_recurrence_id=recurrence_key,
        )
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
        if type(payload) is not dict:
            raise TypeError("recurrence action payload must be an exact dict")
        payload = _detached_exact_key_dict(
            payload,
            label="recurrence action payload",
        )
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

        handler = self._handler_resolver(state.action_id)
        current, current_payload = self._required(recurrence_id)
        if current.status is not RecurrenceStatus.ACTIVE:
            return
        if (
            current.next_due_at != state.next_due_at
            or current.next_occurrence_id != state.next_occurrence_id
        ):
            return
        now = self._now()
        if current.deadline_at is not None and now >= current.deadline_at:
            self._complete_terminal(
                current,
                current_payload,
                reason=RecurrenceTerminalReason.DEADLINE,
            )
            return
        if current.next_due_at is None or current.next_occurrence_id is None:
            raise ValueError("active recurrence is missing its next durable intent")
        if now < current.next_due_at:
            return
        invocation = RecurrenceInvocation(
            recurrence_id=current.recurrence_id,
            occurrence_id=current.next_occurrence_id,
            scheduled_for=current.next_due_at,
            payload=dict(current_payload),
        )
        decision = handler(invocation)
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
        if current.status in {RecurrenceStatus.CANCELLED, RecurrenceStatus.COMPLETED}:
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

        try:
            next_due = _first_future_slot(
                invocation.scheduled_for,
                interval_seconds=current.interval_seconds,
                now=now,
            )
        except OverflowError:
            completed = replace(
                current,
                status=RecurrenceStatus.COMPLETED,
                next_due_at=None,
                next_occurrence_id=None,
                last_completed_due_at=invocation.scheduled_for,
                last_completed_occurrence_id=invocation.occurrence_id,
                terminal_reason=RecurrenceTerminalReason.RANGE_EXHAUSTED,
            )
            self._persist(completed, payload)
            return completed
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
        return self._decode_with_task_authority(
            job,
            expected_recurrence_id=recurrence_key,
        )

    def _decode_with_task_authority(
        self,
        job: ScheduledJob,
        *,
        expected_recurrence_id: str,
    ) -> tuple[RecurrenceState, dict[str, Any]]:
        state, payload = _decode_job(
            job,
            expected_recurrence_id=expected_recurrence_id,
            allow_disabled_active=True,
        )
        expected_enabled = (
            state.status is RecurrenceStatus.ACTIVE and state.next_due_at is not None
        )
        if job.enabled == expected_enabled:
            return state, payload

        task_state = self._jobs.task_state(state.task_id)
        if (
            task_state is not None
            and task_state not in _IRREVERSIBLY_TERMINAL_TASK_STATES
        ):
            raise ValueError(
                "durable recurrence enabled state does not match lifecycle state"
            )

        cancelled = replace(
            state,
            status=RecurrenceStatus.CANCELLED,
            next_due_at=None,
            next_occurrence_id=None,
            terminal_reason=None,
        )
        self._persist(cancelled, payload)
        return cancelled, payload

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
                _TASK_ID_KEY: state.task_id,
                IMMUTABLE_JOB_BINDING_KEY: _definition_fingerprint(
                    recurrence_id=state.recurrence_id,
                    task_id=state.task_id,
                    action_id=state.action_id,
                    interval_seconds=state.interval_seconds,
                    anchor_at=state.anchor_at,
                    deadline_at=state.deadline_at,
                    target_payload=target_payload,
                ),
                _RECURRENCE_PAYLOAD_KEY: _encode_state(state),
                _TARGET_PAYLOAD_KEY: dict(target_payload),
            },
            enabled=enabled,
            coalesce=True,
            max_instances=1,
            misfire_grace_seconds=None,
        )
        try:
            self._scheduler.upsert(job)
        except ValueError as exc:
            if "immutable binding conflict" in str(exc):
                raise ValueError(
                    "recurrence_id is already bound to a different recurrence"
                ) from exc
            raise

    def _now(self) -> datetime:
        return _require_aware_utc(self._clock(), "clock value")


def _encode_state(state: RecurrenceState) -> dict[str, Any]:
    return {
        "version": _RECURRENCE_VERSION,
        "recurrence_id": state.recurrence_id,
        "task_id": state.task_id,
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
    allow_disabled_active: bool = False,
) -> tuple[RecurrenceState, dict[str, Any]]:
    if type(job.job_id) is not str or job.job_id != _job_id(expected_recurrence_id):
        raise ValueError("durable recurrence job identity is corrupt")
    if type(job.action_id) is not str or job.action_id != DurableRecurrenceService.ACTION_ID:
        raise ValueError("durable recurrence job has an unexpected action_id")
    if job.trigger_kind is not TriggerKind.DATE:
        raise ValueError("durable recurrence job must use a DATE trigger")
    if type(job.trigger) is not dict:
        raise ValueError("durable recurrence trigger shape is corrupt")
    trigger = _detached_exact_key_dict(
        job.trigger,
        label="durable recurrence trigger",
    )
    if len(trigger) != 1 or "run_date" not in trigger:
        raise ValueError("durable recurrence trigger shape is corrupt")
    if type(job.coalesce) is not bool or job.coalesce is not True:
        raise ValueError("durable recurrence coalesce policy is corrupt")
    if type(job.max_instances) is not int or job.max_instances != 1:
        raise ValueError("durable recurrence max_instances policy is corrupt")
    if job.misfire_grace_seconds is not None:
        raise ValueError("durable recurrence misfire policy is corrupt")
    if type(job.payload) is not dict:
        raise TypeError("durable recurrence payload is corrupt")
    persisted_payload = _detached_exact_key_dict(
        job.payload,
        label="durable recurrence payload",
    )
    scheduled_recurrence_id = _required_text(
        persisted_payload.get("recurrence_id"),
        "scheduled recurrence_id",
    )
    if scheduled_recurrence_id != expected_recurrence_id:
        raise ValueError("durable recurrence scheduled identity mismatch")
    metadata = persisted_payload.get(_RECURRENCE_PAYLOAD_KEY)
    target_payload = persisted_payload.get(_TARGET_PAYLOAD_KEY)
    if type(metadata) is not dict or type(target_payload) is not dict:
        raise TypeError("durable recurrence payload is corrupt")
    metadata = _detached_exact_key_dict(
        metadata,
        label="durable recurrence metadata",
    )
    target_payload = _canonical_payload(target_payload)
    version = metadata.get("version")
    if type(version) is not int or version != _RECURRENCE_VERSION:
        raise ValueError("unsupported durable recurrence payload version")
    recurrence_id = _required_text(metadata.get("recurrence_id"), "persisted recurrence_id")
    if recurrence_id != expected_recurrence_id:
        raise ValueError("durable recurrence identity mismatch")
    task_id = _canonical_task_id(metadata.get("task_id"), label="persisted task_id")
    top_level_task_id = _canonical_task_id(
        persisted_payload.get(_TASK_ID_KEY),
        label="scheduled task_id",
    )
    if top_level_task_id != task_id:
        raise ValueError("durable recurrence task identity mismatch")
    interval = _validate_interval(metadata.get("interval_seconds"))
    anchor = _parse_iso(metadata.get("anchor_at"), "anchor_at")
    _validate_interval_origin(anchor, interval, "persisted anchor_at")
    deadline = _parse_optional_iso(metadata.get("deadline_at"), "deadline_at")
    next_due = _parse_optional_iso(metadata.get("next_due_at"), "next_due_at")
    last_due = _parse_optional_iso(metadata.get("last_completed_due_at"), "last_completed_due_at")
    status_raw = metadata.get("status")
    policy_raw = metadata.get("missed_run_policy")
    if type(status_raw) is not str or type(policy_raw) is not str:
        raise ValueError("durable recurrence enum state is corrupt")
    try:
        status = RecurrenceStatus(status_raw)
        policy = MissedRunPolicy(policy_raw)
    except ValueError as exc:
        raise ValueError("durable recurrence enum state is corrupt") from exc
    terminal_raw = metadata.get("terminal_reason")
    if terminal_raw is not None and type(terminal_raw) is not str:
        raise ValueError("durable recurrence terminal reason is corrupt")
    try:
        terminal_reason = (
            RecurrenceTerminalReason(terminal_raw) if terminal_raw is not None else None
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("durable recurrence terminal reason is corrupt") from exc
    next_id = metadata.get("next_occurrence_id")
    last_id = metadata.get("last_completed_occurrence_id")
    if next_id is not None and (type(next_id) is not str or not next_id.strip()):
        raise ValueError("durable recurrence next occurrence identity is corrupt")
    if last_id is not None and (type(last_id) is not str or not last_id.strip()):
        raise ValueError("durable recurrence completion identity is corrupt")
    if (next_due is None) != (next_id is None):
        raise ValueError("durable recurrence next intent is incomplete")
    if (last_due is None) != (last_id is None):
        raise ValueError("durable recurrence completion cursor is incomplete")
    _validate_persisted_timeline(
        anchor=anchor,
        interval_seconds=interval,
        next_due=next_due,
        last_due=last_due,
    )
    _validate_persisted_deadline(
        anchor=anchor,
        deadline=deadline,
        status=status,
        terminal_reason=terminal_reason,
        next_due=next_due,
        last_due=last_due,
    )
    if next_due is not None and next_id != _occurrence_id(recurrence_id, next_due):
        raise ValueError("durable recurrence next occurrence identity is corrupt")
    if last_due is not None and last_id != _occurrence_id(recurrence_id, last_due):
        raise ValueError("durable recurrence completion identity is corrupt")
    if status in {RecurrenceStatus.ACTIVE, RecurrenceStatus.PAUSED} and next_due is None:
        raise ValueError("non-terminal recurrence is missing its next durable intent")
    if status in {RecurrenceStatus.CANCELLED, RecurrenceStatus.COMPLETED} and next_due is not None:
        raise ValueError("terminal recurrence cannot retain a next durable intent")
    if status is RecurrenceStatus.COMPLETED:
        if terminal_reason is None:
            raise ValueError("completed recurrence is missing its terminal reason")
        if (
            terminal_reason
            in {
                RecurrenceTerminalReason.CONDITION_MET,
                RecurrenceTerminalReason.RANGE_EXHAUSTED,
            }
            and last_due is None
        ):
            raise ValueError("completed recurrence terminal reason requires completion evidence")
    elif terminal_reason is not None:
        raise ValueError("non-completed recurrence cannot retain a terminal reason")
    if type(job.enabled) is not bool:
        raise ValueError("durable recurrence enabled state is corrupt")
    expected_enabled = status is RecurrenceStatus.ACTIVE and next_due is not None
    if job.enabled != expected_enabled:
        recoverable_disabled_active = (
            allow_disabled_active and expected_enabled and job.enabled is False
        )
        if not recoverable_disabled_active:
            raise ValueError(
                "durable recurrence enabled state does not match lifecycle state"
            )
    expected_run_date = next_due or last_due or anchor
    run_date_raw = trigger.get("run_date")
    if type(run_date_raw) is not str or run_date_raw != _iso(expected_run_date):
        raise ValueError("durable recurrence trigger run_date is corrupt")
    state = RecurrenceState(
        recurrence_id=recurrence_id,
        task_id=task_id,
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
    )
    persisted_binding = persisted_payload.get(IMMUTABLE_JOB_BINDING_KEY)
    expected_binding = _definition_fingerprint(
        recurrence_id=state.recurrence_id,
        task_id=state.task_id,
        action_id=state.action_id,
        interval_seconds=state.interval_seconds,
        anchor_at=state.anchor_at,
        deadline_at=state.deadline_at,
        target_payload=target_payload,
    )
    if type(persisted_binding) is not str or persisted_binding != expected_binding:
        raise ValueError("durable recurrence immutable binding is missing or corrupt")
    return state, dict(target_payload)


def _definition_fingerprint(
    *,
    recurrence_id: str,
    task_id: str,
    action_id: str,
    interval_seconds: int,
    anchor_at: datetime,
    deadline_at: datetime | None,
    target_payload: dict[str, Any],
) -> str:
    material = json.dumps(
        {
            "recurrence_id": recurrence_id,
            "task_id": task_id,
            "action_id": action_id,
            "interval_seconds": interval_seconds,
            "anchor_at": _iso(anchor_at),
            "deadline_at": _iso(deadline_at) if deadline_at is not None else None,
            "target_payload": target_payload,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


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
    material = f"{recurrence_id}\0{_iso(scheduled_for)}".encode()
    return "recurrence-occurrence-v1:" + hashlib.sha256(material).hexdigest()


def _job_id(recurrence_id: str) -> str:
    digest = hashlib.sha256(recurrence_id.encode("utf-8")).hexdigest()
    return f"recurrence-v1:{digest}"


def _validate_interval(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError("interval_seconds must be a positive integer")
    return value


def _validate_persisted_timeline(
    *,
    anchor: datetime,
    interval_seconds: int,
    next_due: datetime | None,
    last_due: datetime | None,
) -> None:
    interval = timedelta(seconds=interval_seconds)
    for label, due_at in (
        ("next_due_at", next_due),
        ("last_completed_due_at", last_due),
    ):
        if due_at is None:
            continue
        offset = due_at - anchor
        if offset < timedelta(0) or offset % interval:
            raise ValueError(f"durable recurrence {label} is outside the recurrence grid")
    if next_due is not None and last_due is not None and next_due <= last_due:
        raise ValueError("durable recurrence cursor chronology is corrupt")


def _validate_persisted_deadline(
    *,
    anchor: datetime,
    deadline: datetime | None,
    status: RecurrenceStatus,
    terminal_reason: RecurrenceTerminalReason | None,
    next_due: datetime | None,
    last_due: datetime | None,
) -> None:
    if terminal_reason is RecurrenceTerminalReason.DEADLINE and deadline is None:
        raise ValueError("durable recurrence deadline terminal reason is missing deadline")
    if deadline is None:
        return
    if next_due is not None and next_due >= deadline:
        raise ValueError("durable recurrence next intent must be before deadline")
    if last_due is not None and last_due >= deadline:
        raise ValueError("durable recurrence completion cursor must be before deadline")
    if deadline <= anchor and (
        status is not RecurrenceStatus.COMPLETED
        or terminal_reason is not RecurrenceTerminalReason.DEADLINE
        or last_due is not None
    ):
        raise ValueError("durable recurrence deadline lifecycle is corrupt")


def _validate_interval_origin(
    origin: datetime,
    interval_seconds: int,
    label: str,
) -> None:
    try:
        origin + timedelta(seconds=interval_seconds)
    except OverflowError as exc:
        raise ValueError(
            f"interval_seconds cannot advance {label} within the datetime range"
        ) from exc


def _parse_iso(value: object, label: str) -> datetime:
    if type(value) is not str:
        raise TypeError(f"{label} must be a timezone-aware ISO-8601 string")
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
    if type(value) is not datetime:
        raise ValueError(f"{label} must be timezone-aware")
    timezone_info = value.tzinfo
    if timezone_info is None:
        raise ValueError(f"{label} must be timezone-aware")
    if type(timezone_info) is not type(UTC):
        raise ValueError(f"{label} timezone must be a canonical fixed offset")
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _detached_exact_key_dict(
    value: dict[str, Any],
    *,
    label: str,
) -> dict[str, Any]:
    detached: dict[str, Any] = {}
    for key, item in value.items():
        if type(key) is not str:
            raise TypeError(f"{label} keys must be exact strings")
        utf8_key = _require_utf8_text(key, f"{label} key")
        detached[utf8_key] = item
    return detached


def _canonical_task_id(value: object, *, label: str = "task_id") -> str:
    if type(value) is not str or not value:
        raise ValueError(f"{label} is required")
    if value != value.strip():
        raise ValueError(f"{label} must be canonical")
    return _require_utf8_text(value, label)


def _required_text(value: object, label: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{label} is required")
    return _require_utf8_text(value.strip(), label)


def _require_utf8_text(value: str, label: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{label} must be valid UTF-8 text") from exc
    return value


def _canonical_payload(payload: dict[str, Any] | None) -> dict[str, Any]:
    if payload is None:
        return {}
    if type(payload) is not dict:
        raise TypeError("payload must be an exact dict")
    detached = cast(
        dict[str, Any],
        _canonical_json_value(payload, label="payload", depth=0),
    )
    encoded = json.dumps(
        detached,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > _MAX_PAYLOAD_JSON_BYTES:
        raise ValueError("payload exceeds durable JSON size limit")
    return detached


def _canonical_json_value(value: Any, *, label: str, depth: int) -> Any:
    if depth > _MAX_PAYLOAD_DEPTH:
        raise ValueError(f"{label} exceeds durable JSON nesting limit")
    if value is None or type(value) is bool or type(value) is int:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{label} contains a non-finite number")
        return value
    if type(value) is str:
        return _require_utf8_text(value, label)
    if type(value) is list:
        return [
            _canonical_json_value(item, label=label, depth=depth + 1)
            for item in value
        ]
    if type(value) is dict:
        detached: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError(f"{label} keys must be exact strings")
            utf8_key = _require_utf8_text(key, f"{label} key")
            detached[utf8_key] = _canonical_json_value(
                item,
                label=label,
                depth=depth + 1,
            )
        return detached
    raise TypeError(f"{label} must contain only exact JSON-compatible values")


def _utc_now() -> datetime:
    return datetime.now(UTC)
