from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.runtime.retry import (
    RetryPolicy,
    ScriptRetryCondition,
    ScriptRetryDisposition,
    ScriptRetryIntent,
    evaluate_script_retry_intent,
    plan_script_retry,
)
from nika_core.scheduler import ScheduledJob, ScheduledJobStore, SchedulerPort, TriggerKind

_CONNECTIVITY_WAIT_VERSION = 1
_TERMINAL_RETRY_DISPOSITIONS = frozenset(
    {
        ScriptRetryDisposition.NOT_RETRYABLE,
        ScriptRetryDisposition.ATTEMPTS_EXHAUSTED,
        ScriptRetryDisposition.BACKOFF_LIMIT_EXCEEDED,
        ScriptRetryDisposition.DEADLINE_EXCEEDED,
    }
)


class ConnectivityProbePort(Protocol):
    """Host-supplied connectivity observation used before any external effect."""

    def is_available(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class ConnectivityWaitDecision:
    disposition: ScriptRetryDisposition
    continuation_granted: bool
    intent: ScriptRetryIntent | None = None


@dataclass(frozen=True, slots=True)
class _WaitBinding:
    task_id: str
    operation_id: str
    intent: ScriptRetryIntent


class ConnectivityWaitService:
    """Crash-durable pre-effect network wait composed from existing Nika contracts.

    This service never performs the external effect. It owns only durable connectivity
    retry intent and the WAITING_TOOL -> RETRYING continuation grant. The effect caller
    must still revalidate current approval/effect authority before any external action.
    """

    def __init__(
        self,
        *,
        queue: TaskQueue,
        jobs: ScheduledJobStore,
        audit: AuditLog,
        probe: ConnectivityProbePort,
        scheduler: SchedulerPort | None = None,
    ) -> None:
        self._queue = queue
        self._jobs = jobs
        self._audit = audit
        self._probe = probe
        self._scheduler = scheduler

    def defer(
        self,
        *,
        task_id: str,
        job_id: str,
        action_id: str,
        intent: ScriptRetryIntent,
    ) -> None:
        if not isinstance(intent, ScriptRetryIntent):
            raise TypeError("intent must be a ScriptRetryIntent")
        if intent.condition is not ScriptRetryCondition.RECOVERABLE_NETWORK_FAILURE:
            raise ValueError("connectivity wait requires a recoverable network retry intent")

        job = _job_from_intent(
            job_id=job_id,
            action_id=action_id,
            task_id=task_id,
            intent=intent,
        )
        with self._queue.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._queue.transition_with_connection(conn, task_id, TaskState.WAITING_TOOL)
            self._jobs.upsert_with_connection(conn, job)
            self._audit.append_with_connection(
                conn,
                event_type="runtime.connectivity_wait_deferred",
                entity_type="scheduled_job",
                entity_id=job_id,
                payload=_audit_payload(task_id=task_id, intent=intent),
            )

        # Durable DB state is already authoritative. If runtime installation fails, expose
        # that failure to the caller; scheduler startup can rehydrate the enabled DATE job.
        self._activate_runtime(job)

    def evaluate(
        self,
        *,
        job_id: str,
        policy: RetryPolicy,
        now: datetime,
        replay_safe: bool,
    ) -> ConnectivityWaitDecision:
        current_time = _as_utc(now)
        outer_job = self._jobs.get(job_id)
        if outer_job is None:
            return ConnectivityWaitDecision(ScriptRetryDisposition.NOT_RETRYABLE, False)
        try:
            outer_binding = _decode_binding(outer_job)
        except (TypeError, ValueError):
            self._reject_malformed(job_id)
            return ConnectivityWaitDecision(ScriptRetryDisposition.NOT_RETRYABLE, False)
        if not outer_job.enabled:
            return ConnectivityWaitDecision(
                ScriptRetryDisposition.PAUSED,
                False,
                outer_binding.intent,
            )

        task_state = _read_task_state(self._queue.store, outer_binding.task_id)
        if task_state is None:
            self._reject_malformed(job_id, reason="missing_task")
            return ConnectivityWaitDecision(ScriptRetryDisposition.NOT_RETRYABLE, False)
        if task_state is TaskState.CANCELLED:
            return self._disable_terminal(
                job_id=job_id,
                binding=outer_binding,
                disposition=ScriptRetryDisposition.CANCELLED,
                reason="task_cancelled",
            )
        if task_state is not TaskState.WAITING_TOOL:
            self._reject_malformed(job_id, reason="task_state_mismatch")
            return ConnectivityWaitDecision(
                ScriptRetryDisposition.NOT_RETRYABLE,
                False,
                outer_binding.intent,
            )

        initial = evaluate_script_retry_intent(
            outer_binding.intent,
            policy,
            now=current_time,
            replay_safe=replay_safe,
        )
        if initial.disposition is ScriptRetryDisposition.WAITING:
            return ConnectivityWaitDecision(
                ScriptRetryDisposition.WAITING,
                False,
                outer_binding.intent,
            )
        if initial.disposition in _TERMINAL_RETRY_DISPOSITIONS:
            return self._disable_terminal(
                job_id=job_id,
                binding=outer_binding,
                disposition=initial.disposition,
                reason="retry_authority_terminal",
                block_waiting_task=True,
            )
        if initial.disposition is not ScriptRetryDisposition.READY:
            return self._disable_terminal(
                job_id=job_id,
                binding=outer_binding,
                disposition=initial.disposition,
                reason="retry_authority_not_ready",
            )

        # Observe before the SQLite write claim so simultaneous wake callers contend on
        # canonical durable authority rather than an in-process ownership flag.
        initially_available = self._probe.is_available()
        runtime_job: ScheduledJob | None = None
        with self._queue.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = self._jobs.get_with_connection(conn, job_id)
            if job is None:
                return ConnectivityWaitDecision(ScriptRetryDisposition.NOT_RETRYABLE, False)
            try:
                binding = _decode_binding(job)
            except (TypeError, ValueError):
                self._jobs.set_enabled_with_connection(conn, job_id, False)
                self._audit_rejected_with_connection(conn, job_id, reason="invalid_payload")
                return ConnectivityWaitDecision(ScriptRetryDisposition.NOT_RETRYABLE, False)
            if not job.enabled:
                return ConnectivityWaitDecision(
                    ScriptRetryDisposition.PAUSED,
                    False,
                    binding.intent,
                )
            if not _same_binding(binding, outer_binding):
                return ConnectivityWaitDecision(
                    ScriptRetryDisposition.WAITING,
                    False,
                    binding.intent,
                )

            state = _task_state_with_connection(conn, binding.task_id)
            if state is None:
                self._jobs.set_enabled_with_connection(conn, job_id, False)
                self._audit_rejected_with_connection(conn, job_id, reason="missing_task")
                return ConnectivityWaitDecision(ScriptRetryDisposition.NOT_RETRYABLE, False)
            if state is TaskState.CANCELLED:
                self._jobs.set_enabled_with_connection(conn, job_id, False)
                self._audit.append_with_connection(
                    conn,
                    event_type="runtime.connectivity_wait_cancelled",
                    entity_type="scheduled_job",
                    entity_id=job_id,
                    payload=_audit_payload(task_id=binding.task_id, intent=binding.intent),
                )
                return ConnectivityWaitDecision(
                    ScriptRetryDisposition.CANCELLED,
                    False,
                    binding.intent,
                )
            if state is not TaskState.WAITING_TOOL:
                self._jobs.set_enabled_with_connection(conn, job_id, False)
                self._audit_rejected_with_connection(conn, job_id, reason="task_state_mismatch")
                return ConnectivityWaitDecision(
                    ScriptRetryDisposition.NOT_RETRYABLE,
                    False,
                    binding.intent,
                )

            fresh = evaluate_script_retry_intent(
                binding.intent,
                policy,
                now=current_time,
                replay_safe=replay_safe,
            )
            if fresh.disposition is ScriptRetryDisposition.WAITING:
                return ConnectivityWaitDecision(
                    ScriptRetryDisposition.WAITING,
                    False,
                    binding.intent,
                )
            if fresh.disposition in _TERMINAL_RETRY_DISPOSITIONS:
                self._queue.transition_with_connection(conn, binding.task_id, TaskState.BLOCKED)
                self._jobs.set_enabled_with_connection(conn, job_id, False)
                self._audit.append_with_connection(
                    conn,
                    event_type="runtime.connectivity_wait_blocked",
                    entity_type="scheduled_job",
                    entity_id=job_id,
                    payload={
                        **_audit_payload(task_id=binding.task_id, intent=binding.intent),
                        "disposition": fresh.disposition.value,
                    },
                )
                return ConnectivityWaitDecision(fresh.disposition, False, binding.intent)
            if fresh.disposition is not ScriptRetryDisposition.READY:
                self._jobs.set_enabled_with_connection(conn, job_id, False)
                self._audit_rejected_with_connection(conn, job_id, reason="retry_not_ready")
                return ConnectivityWaitDecision(fresh.disposition, False, binding.intent)

            available_now = initially_available and self._probe.is_available()
            if available_now:
                self._queue.transition_with_connection(conn, binding.task_id, TaskState.RETRYING)
                self._jobs.set_enabled_with_connection(conn, job_id, False)
                self._audit.append_with_connection(
                    conn,
                    event_type="runtime.connectivity_wait_ready",
                    entity_type="scheduled_job",
                    entity_id=job_id,
                    payload=_audit_payload(task_id=binding.task_id, intent=binding.intent),
                )
                return ConnectivityWaitDecision(
                    ScriptRetryDisposition.READY,
                    True,
                    binding.intent,
                )

            next_decision = plan_script_retry(
                policy,
                operation_id=binding.intent.operation_id,
                condition=binding.intent.condition,
                retries_used=binding.intent.retry_number,
                now=current_time,
                replay_safe=replay_safe,
                deadline=binding.intent.deadline_utc,
            )
            if (
                next_decision.disposition is ScriptRetryDisposition.SCHEDULED
                and next_decision.intent is not None
            ):
                runtime_job = _job_from_intent(
                    job_id=job.job_id,
                    action_id=job.action_id,
                    task_id=binding.task_id,
                    intent=next_decision.intent,
                    coalesce=job.coalesce,
                    max_instances=job.max_instances,
                    misfire_grace_seconds=job.misfire_grace_seconds,
                )
                self._jobs.upsert_with_connection(conn, runtime_job)
                self._audit.append_with_connection(
                    conn,
                    event_type="runtime.connectivity_wait_rescheduled",
                    entity_type="scheduled_job",
                    entity_id=job_id,
                    payload=_audit_payload(
                        task_id=binding.task_id,
                        intent=next_decision.intent,
                    ),
                )
                result = ConnectivityWaitDecision(
                    ScriptRetryDisposition.SCHEDULED,
                    False,
                    next_decision.intent,
                )
            else:
                if next_decision.disposition in _TERMINAL_RETRY_DISPOSITIONS:
                    self._queue.transition_with_connection(conn, binding.task_id, TaskState.BLOCKED)
                self._jobs.set_enabled_with_connection(conn, job_id, False)
                self._audit.append_with_connection(
                    conn,
                    event_type="runtime.connectivity_wait_blocked",
                    entity_type="scheduled_job",
                    entity_id=job_id,
                    payload={
                        **_audit_payload(task_id=binding.task_id, intent=binding.intent),
                        "disposition": next_decision.disposition.value,
                    },
                )
                result = ConnectivityWaitDecision(
                    next_decision.disposition,
                    False,
                    binding.intent,
                )

        if runtime_job is not None:
            self._activate_runtime(runtime_job)
        return result

    def _disable_terminal(
        self,
        *,
        job_id: str,
        binding: _WaitBinding,
        disposition: ScriptRetryDisposition,
        reason: str,
        block_waiting_task: bool = False,
    ) -> ConnectivityWaitDecision:
        with self._queue.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = self._jobs.get_with_connection(conn, job_id)
            if job is None:
                return ConnectivityWaitDecision(disposition, False, binding.intent)
            state = _task_state_with_connection(conn, binding.task_id)
            if block_waiting_task and state is TaskState.WAITING_TOOL:
                self._queue.transition_with_connection(conn, binding.task_id, TaskState.BLOCKED)
            self._jobs.set_enabled_with_connection(conn, job_id, False)
            self._audit.append_with_connection(
                conn,
                event_type="runtime.connectivity_wait_blocked",
                entity_type="scheduled_job",
                entity_id=job_id,
                payload={
                    **_audit_payload(task_id=binding.task_id, intent=binding.intent),
                    "disposition": disposition.value,
                    "reason": reason,
                },
            )
        return ConnectivityWaitDecision(disposition, False, binding.intent)

    def _reject_malformed(self, job_id: str, *, reason: str = "invalid_payload") -> None:
        with self._queue.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._jobs.set_enabled_with_connection(conn, job_id, False)
            self._audit_rejected_with_connection(conn, job_id, reason=reason)

    def _audit_rejected_with_connection(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        *,
        reason: str,
    ) -> None:
        self._audit.append_with_connection(
            conn,
            event_type="runtime.connectivity_wait_rejected",
            entity_type="scheduled_job",
            entity_id=job_id,
            payload={"reason": reason},
        )

    def _activate_runtime(self, job: ScheduledJob) -> None:
        if self._scheduler is not None:
            self._scheduler.upsert(job)


def _job_from_intent(
    *,
    job_id: str,
    action_id: str,
    task_id: str,
    intent: ScriptRetryIntent,
    coalesce: bool = True,
    max_instances: int = 1,
    misfire_grace_seconds: int | None = 60,
) -> ScheduledJob:
    return ScheduledJob(
        job_id=job_id,
        action_id=action_id,
        trigger_kind=TriggerKind.DATE,
        trigger={"run_date": intent.not_before_utc.isoformat()},
        payload={
            "connectivity_wait_version": _CONNECTIVITY_WAIT_VERSION,
            "task_id": task_id,
            "operation_id": intent.operation_id,
            "script_retry_intent": intent.to_payload(),
        },
        enabled=True,
        coalesce=coalesce,
        max_instances=max_instances,
        misfire_grace_seconds=misfire_grace_seconds,
    )


def _decode_binding(job: ScheduledJob) -> _WaitBinding:
    if job.trigger_kind is not TriggerKind.DATE:
        raise ValueError("connectivity wait requires a DATE job")
    payload = job.payload
    expected = {
        "connectivity_wait_version",
        "task_id",
        "operation_id",
        "script_retry_intent",
    }
    if set(payload) != expected:
        raise ValueError("connectivity wait payload fields are invalid")
    version = payload["connectivity_wait_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != _CONNECTIVITY_WAIT_VERSION:
        raise ValueError("connectivity wait payload version is unsupported")
    task_id = payload["task_id"]
    operation_id = payload["operation_id"]
    intent_payload = payload["script_retry_intent"]
    if not isinstance(task_id, str) or not task_id or task_id != task_id.strip():
        raise ValueError("task_id must be canonical non-empty text")
    if not isinstance(operation_id, str) or not operation_id or operation_id != operation_id.strip():
        raise ValueError("operation_id must be canonical non-empty text")
    if not isinstance(intent_payload, Mapping):
        raise TypeError("script_retry_intent must be a mapping")
    intent = ScriptRetryIntent.from_payload(intent_payload)
    if intent.operation_id != operation_id:
        raise ValueError("retry intent operation identity mismatch")
    expected_trigger = {"run_date": intent.not_before_utc.isoformat()}
    if job.trigger != expected_trigger:
        raise ValueError("retry wake time does not match durable retry intent")
    return _WaitBinding(task_id=task_id, operation_id=operation_id, intent=intent)


def _same_binding(left: _WaitBinding, right: _WaitBinding) -> bool:
    return (
        left.task_id == right.task_id
        and left.operation_id == right.operation_id
        and left.intent.to_payload() == right.intent.to_payload()
    )


def _as_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value.astimezone(UTC)


def _read_task_state(store: object, task_id: str) -> TaskState | None:
    with store.connection() as conn:
        return _task_state_with_connection(conn, task_id)


def _task_state_with_connection(conn: sqlite3.Connection, task_id: str) -> TaskState | None:
    row = conn.execute("SELECT state FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    return TaskState(row["state"]) if row is not None else None


def _audit_payload(*, task_id: str, intent: ScriptRetryIntent) -> dict[str, object]:
    return {
        "task_id": task_id,
        "operation_id": intent.operation_id,
        "condition": intent.condition.value,
        "retry_number": intent.retry_number,
    }
