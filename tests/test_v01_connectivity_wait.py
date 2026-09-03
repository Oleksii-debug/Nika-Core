from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier, Lock

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.runtime.connectivity_wait import ConnectivityWaitService
from nika_core.runtime.retry import (
    RetryPolicy,
    ScriptRetryCondition,
    ScriptRetryDisposition,
    ScriptRetryIntent,
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


class _RecordingScheduler:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.jobs: list[ScheduledJob] = []

    def start(self) -> None:
        return None

    def shutdown(self, *, wait: bool = True) -> None:
        del wait

    def upsert(self, job: ScheduledJob) -> None:
        if self.fail:
            raise RuntimeError("injected runtime scheduler failure")
        self.jobs.append(job)

    def remove(self, job_id: str) -> bool:
        del job_id
        return False

    def pause(self, job_id: str) -> None:
        del job_id

    def resume(self, job_id: str) -> None:
        del job_id


class _TwoWakeProbe:
    def __init__(self) -> None:
        self._barrier = Barrier(2)
        self._lock = Lock()
        self._calls = 0

    def is_available(self) -> bool:
        with self._lock:
            self._calls += 1
            call = self._calls
        if call <= 2:
            self._barrier.wait(timeout=10)
        return True


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
) -> ScriptRetryIntent:
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


def test_offline_wait_survives_restart_reschedules_and_reconnect_grants_once(tmp_path) -> None:
    db_path = tmp_path / "Ніка Offline Reconnect" / "nika core.db"
    store = SQLiteStore(db_path)
    store.initialize()
    queue = TaskQueue(store)
    jobs = ScheduledJobStore(store)
    audit = AuditLog(store)
    probe = _ConnectivityProbe(available=False)
    scheduler = _RecordingScheduler()
    policy = RetryPolicy(max_retries=3, base_delay_seconds=10, max_delay_seconds=30)
    now = datetime(2026, 9, 3, 5, 0, tzinfo=UTC)
    task_id = _running_task(queue)
    intent = _network_intent(policy, operation_id="network-op-1", now=now)

    service = ConnectivityWaitService(
        queue=queue,
        jobs=jobs,
        audit=audit,
        probe=probe,
        scheduler=scheduler,
    )
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
    assert stored.payload["script_retry_intent"]["operation_id"] == "network-op-1"
    assert "secret_canary" not in repr(stored.payload)
    assert len(scheduler.jobs) == 1

    reopened = SQLiteStore(db_path)
    reopened.initialize()
    restarted_queue = TaskQueue(reopened)
    restarted_jobs = ScheduledJobStore(reopened)
    restarted_audit = AuditLog(reopened)
    restarted_scheduler = _RecordingScheduler()
    restarted = ConnectivityWaitService(
        queue=restarted_queue,
        jobs=restarted_jobs,
        audit=restarted_audit,
        probe=probe,
        scheduler=restarted_scheduler,
    )

    before_backoff = restarted.evaluate(
        job_id="connectivity-network-op-1",
        policy=policy,
        now=now + timedelta(seconds=5),
        replay_safe=True,
    )
    assert before_backoff.disposition is ScriptRetryDisposition.WAITING
    assert before_backoff.continuation_granted is False
    assert restarted_scheduler.jobs == []

    still_offline = restarted.evaluate(
        job_id="connectivity-network-op-1",
        policy=policy,
        now=now + timedelta(seconds=11),
        replay_safe=True,
    )
    assert still_offline.disposition is ScriptRetryDisposition.SCHEDULED
    assert still_offline.continuation_granted is False
    assert still_offline.intent is not None
    assert still_offline.intent.retry_number == 2
    assert restarted_queue.get(task_id).state is TaskState.WAITING_TOOL
    assert restarted_jobs.get("connectivity-network-op-1").enabled is True
    assert len(restarted_scheduler.jobs) == 1
    assert restarted_scheduler.jobs[-1].trigger == {
        "run_date": still_offline.intent.not_before_utc.isoformat()
    }

    probe.available = True
    too_early = restarted.evaluate(
        job_id="connectivity-network-op-1",
        policy=policy,
        now=still_offline.intent.not_before_utc - timedelta(seconds=1),
        replay_safe=True,
    )
    assert too_early.disposition is ScriptRetryDisposition.WAITING
    assert too_early.continuation_granted is False

    connected = restarted.evaluate(
        job_id="connectivity-network-op-1",
        policy=policy,
        now=still_offline.intent.not_before_utc,
        replay_safe=True,
    )
    assert connected.disposition is ScriptRetryDisposition.READY
    assert connected.continuation_granted is True
    assert restarted_queue.get(task_id).state is TaskState.RETRYING
    assert restarted_jobs.get("connectivity-network-op-1").enabled is False

    duplicate = restarted.evaluate(
        job_id="connectivity-network-op-1",
        policy=policy,
        now=still_offline.intent.not_before_utc + timedelta(seconds=1),
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
    assert queue.get(deadline_task).state is TaskState.BLOCKED
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
    assert queue.get(unsafe_task).state is TaskState.BLOCKED
    assert jobs.get("connectivity-unsafe").enabled is False


def test_defer_rolls_back_task_when_durable_job_write_fails(tmp_path, monkeypatch) -> None:
    store = SQLiteStore(tmp_path / "Ніка Connectivity Atomic" / "nika core.db")
    store.initialize()
    queue = TaskQueue(store)
    jobs = ScheduledJobStore(store)
    audit = AuditLog(store)
    now = datetime(2026, 9, 3, 6, 30, tzinfo=UTC)
    policy = RetryPolicy(max_retries=1, base_delay_seconds=1, max_delay_seconds=5)
    task_id = _running_task(queue)
    service = ConnectivityWaitService(
        queue=queue,
        jobs=jobs,
        audit=audit,
        probe=_ConnectivityProbe(available=False),
    )

    def fail_upsert(conn: sqlite3.Connection, job: ScheduledJob) -> None:
        del conn, job
        raise sqlite3.OperationalError("injected durable write failure")

    monkeypatch.setattr(jobs, "upsert_with_connection", fail_upsert)
    with pytest.raises(sqlite3.OperationalError, match="injected durable write failure"):
        service.defer(
            task_id=task_id,
            job_id="connectivity-atomic",
            action_id="runtime.resume_after_connectivity",
            intent=_network_intent(policy, operation_id="atomic-op", now=now),
        )

    assert queue.get(task_id).state is TaskState.RUNNING
    assert jobs.get("connectivity-atomic") is None
    assert audit.list_for(entity_type="scheduled_job", entity_id="connectivity-atomic") == ()


def test_runtime_activation_failure_keeps_durable_wait_and_is_visible(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "Ніка Connectivity Runtime Failure" / "nika core.db")
    store.initialize()
    queue = TaskQueue(store)
    jobs = ScheduledJobStore(store)
    audit = AuditLog(store)
    now = datetime(2026, 9, 3, 6, 45, tzinfo=UTC)
    policy = RetryPolicy(max_retries=1, base_delay_seconds=1, max_delay_seconds=5)
    task_id = _running_task(queue)
    service = ConnectivityWaitService(
        queue=queue,
        jobs=jobs,
        audit=audit,
        probe=_ConnectivityProbe(available=False),
        scheduler=_RecordingScheduler(fail=True),
    )

    with pytest.raises(RuntimeError, match="injected runtime scheduler failure"):
        service.defer(
            task_id=task_id,
            job_id="connectivity-runtime-failure",
            action_id="runtime.resume_after_connectivity",
            intent=_network_intent(policy, operation_id="runtime-failure-op", now=now),
        )

    assert queue.get(task_id).state is TaskState.WAITING_TOOL
    durable = jobs.get("connectivity-runtime-failure")
    assert durable is not None
    assert durable.enabled is True
    assert "НЕ_ЛОГУВАТИ" not in repr(durable.payload)


def test_two_simultaneous_reconnect_wakes_grant_continuation_once(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "Ніка Connectivity Race" / "nika core.db")
    store.initialize()
    queue = TaskQueue(store)
    jobs = ScheduledJobStore(store)
    audit = AuditLog(store)
    now = datetime(2026, 9, 3, 7, 0, tzinfo=UTC)
    policy = RetryPolicy(max_retries=1, base_delay_seconds=0, max_delay_seconds=1)
    task_id = _running_task(queue)
    service = ConnectivityWaitService(
        queue=queue,
        jobs=jobs,
        audit=audit,
        probe=_TwoWakeProbe(),
    )
    service.defer(
        task_id=task_id,
        job_id="connectivity-race",
        action_id="runtime.resume_after_connectivity",
        intent=_network_intent(policy, operation_id="race-op", now=now),
    )

    def wake():
        return service.evaluate(
            job_id="connectivity-race",
            policy=policy,
            now=now,
            replay_safe=True,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda _: wake(), range(2)))

    assert sum(result.continuation_granted for result in results) == 1
    assert queue.get(task_id).state is TaskState.RETRYING
    assert jobs.get("connectivity-race").enabled is False
    ready_events = audit.list_for(entity_type="scheduled_job", entity_id="connectivity-race")
    assert sum(event.event_type == "runtime.connectivity_wait_ready" for event in ready_events) == 1


def test_malformed_wait_payload_fails_closed_without_secret_leak(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "Ніка Connectivity Corruption" / "nika core.db")
    store.initialize()
    queue = TaskQueue(store)
    jobs = ScheduledJobStore(store)
    audit = AuditLog(store)
    now = datetime(2026, 9, 3, 7, 30, tzinfo=UTC)
    policy = RetryPolicy(max_retries=1, base_delay_seconds=1, max_delay_seconds=5)
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

    service = ConnectivityWaitService(
        queue=queue,
        jobs=jobs,
        audit=audit,
        probe=_ConnectivityProbe(available=True),
    )
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
