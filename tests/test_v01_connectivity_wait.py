from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from threading import Barrier, Lock

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.runtime import connectivity_wait as wait_module
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
            now=now + timedelta(seconds=1),
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


@pytest.mark.parametrize("old_state", [TaskState.CANCELLED, TaskState.RETRYING])
def test_stale_terminal_read_does_not_disable_reassigned_job(
    tmp_path, monkeypatch, old_state
) -> None:
    store = SQLiteStore(tmp_path / "Ніка Stale Wake" / "nika core.db")
    store.initialize()
    queue = TaskQueue(store)
    jobs = ScheduledJobStore(store)
    audit = AuditLog(store)
    policy = RetryPolicy(max_retries=2, base_delay_seconds=1, max_delay_seconds=5)
    now = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)
    service = ConnectivityWaitService(
        queue=queue, jobs=jobs, audit=audit, probe=_ConnectivityProbe(available=True)
    )
    old_task = _running_task(queue)
    new_task = _running_task(queue)
    service.defer(
        task_id=old_task,
        job_id="shared-wake",
        action_id="runtime.resume_after_connectivity",
        intent=_network_intent(policy, operation_id="old-op", now=now),
    )
    service.defer(
        task_id=new_task,
        job_id="successor-wake",
        action_id="runtime.resume_after_connectivity",
        intent=_network_intent(policy, operation_id="new-op", now=now),
    )
    queue.transition(old_task, old_state)
    successor = replace(jobs.get("successor-wake"), job_id="shared-wake")
    original_read = wait_module._read_task_state

    def reassign_after_read(current_store, task_id):
        state = original_read(current_store, task_id)
        if task_id == old_task:
            jobs.upsert(successor)
        return state

    monkeypatch.setattr(wait_module, "_read_task_state", reassign_after_read)
    decision = service.evaluate(
        job_id="shared-wake",
        policy=policy,
        now=now + timedelta(seconds=2),
        replay_safe=True,
    )
    assert decision.disposition is ScriptRetryDisposition.WAITING
    assert not decision.continuation_granted
    assert jobs.get("shared-wake") == successor
    assert jobs.get("shared-wake").enabled
    assert queue.get(new_task).state is TaskState.WAITING_TOOL
    events = audit.list_for(entity_type="scheduled_job", entity_id="shared-wake")
    assert not any(
        event.event_type.endswith(("_blocked", "_cancelled", "_rejected"))
        for event in events
    )



def test_malformed_old_snapshot_cannot_disable_new_valid_job(tmp_path, monkeypatch) -> None:
    store = SQLiteStore(tmp_path / "Ніка Corrupt Reassignment" / "nika core.db")
    store.initialize()
    queue = TaskQueue(store)
    jobs = ScheduledJobStore(store)
    audit = AuditLog(store)
    policy = RetryPolicy(max_retries=1, base_delay_seconds=1, max_delay_seconds=5)
    now = datetime(2026, 9, 3, 8, 30, tzinfo=UTC)
    service = ConnectivityWaitService(
        queue=queue, jobs=jobs, audit=audit, probe=_ConnectivityProbe(available=True)
    )
    task_id = _running_task(queue)
    service.defer(
        task_id=task_id,
        job_id="new-job",
        action_id="runtime.resume_after_connectivity",
        intent=_network_intent(policy, operation_id="new-op", now=now),
    )
    successor = replace(jobs.get("new-job"), job_id="reused-job")
    jobs.upsert(replace(successor, payload={"malformed": True}))
    original_decode = wait_module._decode_binding

    def reassign_before_rejection(job):
        if job.payload == {"malformed": True}:
            jobs.upsert(successor)
        return original_decode(job)

    monkeypatch.setattr(wait_module, "_decode_binding", reassign_before_rejection)
    decision = service.evaluate(
        job_id="reused-job",
        policy=policy,
        now=now + timedelta(seconds=2),
        replay_safe=True,
    )
    assert decision.disposition is ScriptRetryDisposition.NOT_RETRYABLE
    assert not decision.continuation_granted
    assert jobs.get("reused-job") == successor
    assert jobs.get("reused-job").enabled
    assert queue.get(task_id).state is TaskState.WAITING_TOOL
    assert not any(
        event.event_type == "runtime.connectivity_wait_rejected"
        for event in audit.list_for(entity_type="scheduled_job", entity_id="reused-job")
    )



def test_defer_refuses_reused_job_id_without_abandoning_original_wait(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "Ніка Unique Wake" / "nika core.db")
    store.initialize()
    queue = TaskQueue(store)
    jobs = ScheduledJobStore(store)
    audit = AuditLog(store)
    policy = RetryPolicy(max_retries=1, base_delay_seconds=1, max_delay_seconds=5)
    now = datetime(2026, 9, 3, 9, 0, tzinfo=UTC)
    service = ConnectivityWaitService(
        queue=queue, jobs=jobs, audit=audit, probe=_ConnectivityProbe(available=False)
    )
    old_task = _running_task(queue)
    new_task = _running_task(queue)
    service.defer(
        task_id=old_task,
        job_id="occupied-job",
        action_id="runtime.resume_after_connectivity",
        intent=_network_intent(policy, operation_id="old-op", now=now),
    )
    incumbent = jobs.get("occupied-job")
    with pytest.raises(ValueError, match="job_id already exists"):
        service.defer(
            task_id=new_task,
            job_id="occupied-job",
            action_id="runtime.resume_after_connectivity",
            intent=_network_intent(policy, operation_id="new-op", now=now),
        )
    assert jobs.get("occupied-job") == incumbent
    assert queue.get(old_task).state is TaskState.WAITING_TOOL
    assert queue.get(new_task).state is TaskState.RUNNING
    events = audit.list_for(entity_type="scheduled_job", entity_id="occupied-job")
    assert [event.event_type for event in events] == ["runtime.connectivity_wait_deferred"]


@pytest.mark.parametrize("swap_on_call", [1, 2])
def test_probe_action_swap_does_not_grant_stale_wake(tmp_path, swap_on_call) -> None:
    store = SQLiteStore(tmp_path / "Ніка Swapped Wake Action" / "nika core.db")
    store.initialize()
    queue = TaskQueue(store)
    jobs = ScheduledJobStore(store)
    audit = AuditLog(store)
    policy = RetryPolicy(max_retries=1, base_delay_seconds=1, max_delay_seconds=5)
    now = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)
    task_id = _running_task(queue)

    class _ReassigningProbe:
        def __init__(self) -> None:
            self.calls = 0

        def is_available(self) -> bool:
            self.calls += 1
            if self.calls == swap_on_call:
                job = jobs.get("action-swap")
                jobs.upsert(replace(job, action_id="runtime.other_action"))
            return True

    service = ConnectivityWaitService(
        queue=queue, jobs=jobs, audit=audit, probe=_ReassigningProbe()
    )
    service.defer(
        task_id=task_id,
        job_id="action-swap",
        action_id="runtime.resume_after_connectivity",
        intent=_network_intent(policy, operation_id="action-swap-op", now=now),
    )
    decision = service.evaluate(
        job_id="action-swap",
        policy=policy,
        now=now + timedelta(seconds=2),
        replay_safe=True,
    )
    assert decision.disposition is ScriptRetryDisposition.WAITING
    assert not decision.continuation_granted
    assert queue.get(task_id).state is TaskState.WAITING_TOOL
    assert jobs.get("action-swap").enabled
    assert jobs.get("action-swap").action_id == "runtime.other_action"
    events = audit.list_for(entity_type="scheduled_job", entity_id="action-swap")
    assert not any(event.event_type == "runtime.connectivity_wait_ready" for event in events)


def test_exact_defer_replay_recovers_failed_runtime_activation(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "Ніка Deferred Resume" / "nika core.db")
    store.initialize()
    queue = TaskQueue(store)
    jobs = ScheduledJobStore(store)
    audit = AuditLog(store)
    policy = RetryPolicy(max_retries=1, base_delay_seconds=1, max_delay_seconds=5)
    now = datetime(2026, 9, 3, 11, 0, tzinfo=UTC)
    task_id = _running_task(queue)
    intent = _network_intent(policy, operation_id="durable-op", now=now)
    args = {
        "task_id": task_id,
        "job_id": "durable-replay",
        "action_id": "runtime.resume_after_connectivity",
        "intent": intent,
    }

    failed = ConnectivityWaitService(
        queue=queue,
        jobs=jobs,
        audit=audit,
        probe=_ConnectivityProbe(available=False),
        scheduler=_RecordingScheduler(fail=True),
    )
    with pytest.raises(RuntimeError, match="injected runtime scheduler failure"):
        failed.defer(**args)
    original_job = jobs.get("durable-replay")
    assert original_job.enabled
    assert queue.get(task_id).state is TaskState.WAITING_TOOL

    healthy_scheduler = _RecordingScheduler()
    recovered = ConnectivityWaitService(
        queue=queue,
        jobs=jobs,
        audit=audit,
        probe=_ConnectivityProbe(available=False),
        scheduler=healthy_scheduler,
    )
    recovered.defer(**args)
    assert jobs.get("durable-replay") == original_job
    assert queue.get(task_id).state is TaskState.WAITING_TOOL
    assert healthy_scheduler.jobs == [original_job]
    events = audit.list_for(entity_type="scheduled_job", entity_id="durable-replay")
    assert [event.event_type for event in events] == ["runtime.connectivity_wait_deferred"]


def test_defer_skips_stale_runtime_activation_after_job_reassignment(
    tmp_path, monkeypatch
) -> None:
    store = SQLiteStore(tmp_path / "Ніка Activation Reassignment" / "nika core.db")
    store.initialize()
    queue = TaskQueue(store)
    jobs = ScheduledJobStore(store)
    audit = AuditLog(store)
    scheduler = _RecordingScheduler()
    policy = RetryPolicy(max_retries=2, base_delay_seconds=1, max_delay_seconds=5)
    now = datetime(2026, 9, 3, 11, 0, tzinfo=UTC)
    task_id = _running_task(queue)
    service = ConnectivityWaitService(
        queue=queue,
        jobs=jobs,
        audit=audit,
        probe=_ConnectivityProbe(available=False),
        scheduler=scheduler,
    )
    original_get = jobs.get
    reassigned = False

    def reassign_before_activation(job_id):
        nonlocal reassigned
        current = original_get(job_id)
        if current is not None and not reassigned:
            jobs.upsert(replace(current, action_id="runtime.successor_action"))
            reassigned = True
        return original_get(job_id)

    monkeypatch.setattr(jobs, "get", reassign_before_activation)
    service.defer(
        task_id=task_id,
        job_id="activation-race",
        action_id="runtime.resume_after_connectivity",
        intent=_network_intent(policy, operation_id="activation-op", now=now),
    )

    assert reassigned
    assert scheduler.jobs == []
    assert original_get("activation-race").action_id == "runtime.successor_action"
    assert queue.get(task_id).state is TaskState.WAITING_TOOL
    events = audit.list_for(entity_type="scheduled_job", entity_id="activation-race")
    assert [event.event_type for event in events] == ["runtime.connectivity_wait_deferred"]


def test_reschedule_skips_stale_runtime_activation_after_job_reassignment(
    tmp_path, monkeypatch
) -> None:
    store = SQLiteStore(tmp_path / "Ніка Reschedule Reassignment" / "nika core.db")
    store.initialize()
    queue = TaskQueue(store)
    jobs = ScheduledJobStore(store)
    audit = AuditLog(store)
    scheduler = _RecordingScheduler()
    policy = RetryPolicy(max_retries=2, base_delay_seconds=1, max_delay_seconds=5)
    now = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
    task_id = _running_task(queue)
    service = ConnectivityWaitService(
        queue=queue,
        jobs=jobs,
        audit=audit,
        probe=_ConnectivityProbe(available=False),
        scheduler=scheduler,
    )
    service.defer(
        task_id=task_id,
        job_id="reschedule-race",
        action_id="runtime.resume_after_connectivity",
        intent=_network_intent(policy, operation_id="reschedule-op", now=now),
    )
    assert len(scheduler.jobs) == 1
    scheduler.jobs.clear()
    original_get = jobs.get
    reads = 0

    def reassign_after_reschedule(job_id):
        nonlocal reads
        reads += 1
        current = original_get(job_id)
        if reads == 2:
            jobs.upsert(replace(current, action_id="runtime.successor_action"))
            return original_get(job_id)
        return current

    monkeypatch.setattr(jobs, "get", reassign_after_reschedule)
    result = service.evaluate(
        job_id="reschedule-race",
        policy=policy,
        now=now + timedelta(seconds=2),
        replay_safe=True,
    )

    assert result.disposition is ScriptRetryDisposition.SCHEDULED
    assert not result.continuation_granted
    assert reads == 2
    assert scheduler.jobs == []
    assert original_get("reschedule-race").action_id == "runtime.successor_action"
    assert queue.get(task_id).state is TaskState.WAITING_TOOL
    events = audit.list_for(entity_type="scheduled_job", entity_id="reschedule-race")
    assert [event.event_type for event in events] == [
        "runtime.connectivity_wait_deferred",
        "runtime.connectivity_wait_rescheduled",
    ]


def test_connectivity_service_prefers_persisted_only_scheduler_activation(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "Ніка Persisted Scheduler Port" / "nika core.db")
    store.initialize()
    queue = TaskQueue(store)
    jobs = ScheduledJobStore(store)
    audit = AuditLog(store)
    policy = RetryPolicy(max_retries=2, base_delay_seconds=1, max_delay_seconds=5)
    now = datetime(2026, 9, 3, 13, 0, tzinfo=UTC)
    task_id = _running_task(queue)

    class _PersistedOnlyScheduler(_RecordingScheduler):
        def upsert(self, job: ScheduledJob) -> None:
            raise AssertionError("connectivity must not use a durable scheduler upsert")

        def activate_persisted(self, job: ScheduledJob) -> None:
            self.jobs.append(job)

    scheduler = _PersistedOnlyScheduler()
    service = ConnectivityWaitService(
        queue=queue,
        jobs=jobs,
        audit=audit,
        probe=_ConnectivityProbe(available=False),
        scheduler=scheduler,
    )
    service.defer(
        task_id=task_id,
        job_id="persisted-activation",
        action_id="runtime.resume_after_connectivity",
        intent=_network_intent(policy, operation_id="persisted-op", now=now),
    )
    assert scheduler.jobs == [jobs.get("persisted-activation")]

    decision = service.evaluate(
        job_id="persisted-activation",
        policy=policy,
        now=now + timedelta(seconds=2),
        replay_safe=True,
    )
    assert decision.disposition is ScriptRetryDisposition.SCHEDULED
    assert not decision.continuation_granted
    assert scheduler.jobs[-1] == jobs.get("persisted-activation")
    assert len(scheduler.jobs) == 2
    assert queue.get(task_id).state is TaskState.WAITING_TOOL
