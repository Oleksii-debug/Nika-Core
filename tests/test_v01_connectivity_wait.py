from __future__ import annotations

from datetime import UTC, datetime, timedelta

from nika_core.runtime.connectivity_wait import ConnectivityWaitService

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.runtime.retry import (
    RetryPolicy,
    ScriptRetryCondition,
    ScriptRetryDisposition,
    plan_script_retry,
)
from nika_core.scheduler import ScheduledJob, ScheduledJobStore, TriggerKind


class _ConnectivityProbe:
    def __init__(self, *, available: bool) -> None:
        self.available = available
        self.calls = 0

    def is_available(self) -> bool:
        self.calls += 1
        return self.available


def _running_task(queue: TaskQueue) -> str:
    task = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "контрольована мережева дія", "secret_canary": "НЕ_ЛОГУВАТИ"},
    )
    queue.transition(task.task_id, TaskState.READY)
    queue.transition(task.task_id, TaskState.RUNNING)
    return task.task_id


def _network_intent(
    policy: RetryPolicy,
    *,
    operation_id: str,
    now: datetime,
    deadline: datetime | None = None,
):
    decision = plan_script_retry(
        policy,
        operation_id=operation_id,
        condition=ScriptRetryCondition.RECOVERABLE_NETWORK_FAILURE,
        retries_used=0,
        now=now,
        replay_safe=True,
        deadline=deadline,
    )
    assert decision.disposition is ScriptRetryDisposition.SCHEDULED
    assert decision.intent is not None
    return decision.intent


def test_offline_wait_survives_restart_and_reconnect_grants_once(tmp_path) -> None:
    db_path = tmp_path / "Ніка Offline Reconnect" / "nika core.db"
    store = SQLiteStore(db_path)
    store.initialize()
    queue = TaskQueue(store)
    jobs = ScheduledJobStore(store)
    audit = AuditLog(store)
    probe = _ConnectivityProbe(available=False)
    policy = RetryPolicy(max_retries=3, base_delay_seconds=10, max_delay_seconds=30)
    now = datetime(2026, 9, 3, 5, 0, tzinfo=UTC)
    task_id = _running_task(queue)
    intent = _network_intent(policy, operation_id="network-op-1", now=now)

    service = ConnectivityWaitService(queue=queue, jobs=jobs, audit=audit, probe=probe)
    service.defer(
        task_id=task_id,
        job_id="connectivity-network-op-1",
        action_id="runtime.resume_after_connectivity",
        intent=intent,
    )

    assert queue.get(task_id).state is TaskState.WAITING_TOOL
    stored = jobs.get("connectivity-network-op-1")
    assert stored is not None
    assert stored.enabled is True
    assert stored.trigger_kind is TriggerKind.DATE
    assert stored.payload["task_id"] == task_id
    assert stored.payload["retry_intent"]["operation_id"] == "network-op-1"
    assert "secret_canary" not in repr(stored.payload)

    reopened = SQLiteStore(db_path)
    reopened.initialize()
    restarted_queue = TaskQueue(reopened)
    restarted_jobs = ScheduledJobStore(reopened)
    restarted_audit = AuditLog(reopened)
    restarted = ConnectivityWaitService(
        queue=restarted_queue,
        jobs=restarted_jobs,
        audit=restarted_audit,
        probe=probe,
    )

    before_backoff = restarted.evaluate(
        job_id="connectivity-network-op-1",
        policy=policy,
        now=now + timedelta(seconds=5),
        replay_safe=True,
    )
    assert before_backoff.disposition is ScriptRetryDisposition.WAITING
    assert before_backoff.continuation_granted is False
    assert restarted_queue.get(task_id).state is TaskState.WAITING_TOOL

    still_offline = restarted.evaluate(
        job_id="connectivity-network-op-1",
        policy=policy,
        now=now + timedelta(seconds=11),
        replay_safe=True,
    )
    assert still_offline.disposition is ScriptRetryDisposition.WAITING
    assert still_offline.continuation_granted is False
    assert restarted_queue.get(task_id).state is TaskState.WAITING_TOOL
    assert restarted_jobs.get("connectivity-network-op-1").enabled is True

    probe.available = True
    connected = restarted.evaluate(
        job_id="connectivity-network-op-1",
        policy=policy,
        now=now + timedelta(seconds=11),
        replay_safe=True,
    )
    assert connected.disposition is ScriptRetryDisposition.READY
    assert connected.continuation_granted is True
    assert restarted_queue.get(task_id).state is TaskState.RETRYING
    assert restarted_jobs.get("connectivity-network-op-1").enabled is False

    duplicate = restarted.evaluate(
        job_id="connectivity-network-op-1",
        policy=policy,
        now=now + timedelta(seconds=12),
        replay_safe=True,
    )
    assert duplicate.continuation_granted is False
    assert restarted_queue.get(task_id).state is TaskState.RETRYING


def test_cancel_deadline_and_replay_safety_dominate_reconnect(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "Ніка Connectivity Authority" / "nika core.db")
    store.initialize()
    queue = TaskQueue(store)
    jobs = ScheduledJobStore(store)
    audit = AuditLog(store)
    probe = _ConnectivityProbe(available=True)
    policy = RetryPolicy(max_retries=2, base_delay_seconds=1, max_delay_seconds=5)
    now = datetime(2026, 9, 3, 6, 0, tzinfo=UTC)
    service = ConnectivityWaitService(queue=queue, jobs=jobs, audit=audit, probe=probe)

    cancelled_task = _running_task(queue)
    service.defer(
        task_id=cancelled_task,
        job_id="connectivity-cancelled",
        action_id="runtime.resume_after_connectivity",
        intent=_network_intent(policy, operation_id="cancelled-op", now=now),
    )
    queue.transition(cancelled_task, TaskState.CANCELLED)
    cancelled = service.evaluate(
        job_id="connectivity-cancelled",
        policy=policy,
        now=now + timedelta(seconds=2),
        replay_safe=True,
    )
    assert cancelled.disposition is ScriptRetryDisposition.CANCELLED
    assert cancelled.continuation_granted is False
    assert jobs.get("connectivity-cancelled").enabled is False

    deadline_task = _running_task(queue)
    service.defer(
        task_id=deadline_task,
        job_id="connectivity-deadline",
        action_id="runtime.resume_after_connectivity",
        intent=_network_intent(
            policy,
            operation_id="deadline-op",
            now=now,
            deadline=now + timedelta(seconds=3),
        ),
    )
    expired = service.evaluate(
        job_id="connectivity-deadline",
        policy=policy,
        now=now + timedelta(seconds=4),
        replay_safe=True,
    )
    assert expired.disposition is ScriptRetryDisposition.DEADLINE_EXCEEDED
    assert expired.continuation_granted is False
    assert queue.get(deadline_task).state is TaskState.WAITING_TOOL
    assert jobs.get("connectivity-deadline").enabled is False

    unsafe_task = _running_task(queue)
    service.defer(
        task_id=unsafe_task,
        job_id="connectivity-unsafe",
        action_id="runtime.resume_after_connectivity",
        intent=_network_intent(policy, operation_id="unsafe-op", now=now),
    )
    unsafe = service.evaluate(
        job_id="connectivity-unsafe",
        policy=policy,
        now=now + timedelta(seconds=2),
        replay_safe=False,
    )
    assert unsafe.disposition is ScriptRetryDisposition.NOT_RETRYABLE
    assert unsafe.continuation_granted is False
    assert queue.get(unsafe_task).state is TaskState.WAITING_TOOL
    assert jobs.get("connectivity-unsafe").enabled is False


def test_malformed_or_mismatched_wait_payload_fails_closed(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "Ніка Connectivity Corruption" / "nika core.db")
    store.initialize()
    queue = TaskQueue(store)
    jobs = ScheduledJobStore(store)
    audit = AuditLog(store)
    probe = _ConnectivityProbe(available=True)
    policy = RetryPolicy(max_retries=1, base_delay_seconds=1, max_delay_seconds=5)
    now = datetime(2026, 9, 3, 7, 0, tzinfo=UTC)
    task_id = _running_task(queue)

    jobs.upsert(
        ScheduledJob(
            job_id="connectivity-corrupt",
            action_id="runtime.resume_after_connectivity",
            trigger_kind=TriggerKind.DATE,
            trigger={"run_date": (now + timedelta(seconds=1)).isoformat()},
            payload={
                "connectivity_wait_version": 1,
                "task_id": task_id,
                "retry_intent": {"version": 1, "operation_id": "missing-fields"},
            },
        )
    )

    service = ConnectivityWaitService(queue=queue, jobs=jobs, audit=audit, probe=probe)
    corrupt = service.evaluate(
        job_id="connectivity-corrupt",
        policy=policy,
        now=now + timedelta(seconds=2),
        replay_safe=True,
    )

    assert corrupt.disposition is ScriptRetryDisposition.NOT_RETRYABLE
    assert corrupt.continuation_granted is False
    assert queue.get(task_id).state is TaskState.RUNNING
    assert jobs.get("connectivity-corrupt").enabled is False

    events = audit.list_for(entity_type="scheduled_job", entity_id="connectivity-corrupt")
    assert events
    assert events[-1].event_type == "runtime.connectivity_wait_rejected"
    assert "НЕ_ЛОГУВАТИ" not in repr(events)
