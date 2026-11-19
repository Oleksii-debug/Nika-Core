from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.packaged_task_scheduler import PackagedTaskScheduler
from nika_core.scheduler.contracts import ScheduledJob, TriggerKind
from nika_core.scheduler.recurrence import (
    RecurrenceInvocation,
    RecurrenceStatus,
    RecurrenceTerminalReason,
)


@dataclass
class Clock:
    value: datetime = datetime(2035, 1, 2, 10, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


def _store(tmp_path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "Ніка — заплановані завдання" / "core database.db")
    store.initialize()
    return store


def _ready(store: SQLiteStore, **kwargs) -> str:
    queue = TaskQueue(store)
    task = queue.create(
        workspace_id=kwargs.get("workspace_id", "default"),
        agent_id=kwargs.get("agent_id", "nika.default"),
        payload={"command": "Прочитати два джерела"},
    )
    queue.transition(task.task_id, TaskState.READY)
    return task.task_id


def _host(store, start_task, clock=None) -> PackagedTaskScheduler:
    return PackagedTaskScheduler(
        store=store, audit=AuditLog(store), start_task=start_task, clock=clock
    )


def _fire(host, task_id: str) -> None:
    state = host.get(task_id)
    assert state is not None
    jobs = host._jobs.list_enabled()
    job = next(j for j in jobs if j.payload.get("recurrence_id") == state.recurrence_id)
    host._scheduler._dispatch(job.job_id, job)


def _complete(store, calls):
    def start(task_id):
        calls.append(task_id)
        queue = TaskQueue(store)
        queue.transition(task_id, TaskState.RUNNING)
        queue.transition(task_id, TaskState.COMPLETED)
        return True

    return start


def test_delayed_intent_does_not_start_early_and_dispatches_one_task(tmp_path) -> None:
    store, clock, calls = _store(tmp_path), Clock(), []
    task_id = _ready(store)
    host = _host(store, _complete(store, calls), clock)
    due = clock.value + timedelta(minutes=5)
    state = host.create(task_id=task_id, start_at=due)
    assert TaskQueue(store).get(task_id).state is TaskState.READY
    _fire(host, task_id)
    assert calls == []
    clock.value = due
    _fire(host, task_id)
    completed = host.get(task_id)
    assert calls == [task_id]
    assert completed.status is RecurrenceStatus.COMPLETED
    assert completed.last_completed_occurrence_id == state.next_occurrence_id
    assert host._jobs.list_enabled() == ()
    assert len(TaskQueue(store).list_recent()) == 1


def test_restart_reinstalls_same_intent_without_replaying_completed_task(tmp_path) -> None:
    store, clock, calls = _store(tmp_path), Clock(), []
    task_id = _ready(store)
    host = _host(store, _complete(store, calls), clock)
    first = host.create(task_id=task_id, start_at=clock.value)
    host.start()
    assert host._scheduler.has_runtime_job(host._jobs.list_enabled()[0].job_id)
    host.shutdown()
    assert calls == []

    reopened = SQLiteStore(store.path)
    reopened.initialize()
    recovered = _host(reopened, _complete(reopened, calls), clock)
    assert recovered.get(task_id) == first
    recovered.start()
    try:
        _fire(recovered, task_id)
    finally:
        recovered.shutdown()
    assert calls == [task_id]
    final = _host(reopened, lambda _: pytest.fail("completed task replay"), clock)
    final.start()
    try:
        assert final.get(task_id).status is RecurrenceStatus.COMPLETED
        assert final._jobs.list_enabled() == ()
    finally:
        final.shutdown()


def test_resource_wait_retries_one_future_slot_without_catchup_storm(tmp_path) -> None:
    store, clock, calls = _store(tmp_path), Clock(), []
    task_id = _ready(store)

    def wait_once(key):
        calls.append(key)
        return False

    host = _host(store, wait_once, clock)
    first = host.create(task_id=task_id, start_at=clock.value, retry_interval_seconds=60)
    clock.value += timedelta(hours=12, seconds=7)
    _fire(host, task_id)
    current = host.get(task_id)
    assert current.status is RecurrenceStatus.ACTIVE
    assert current.last_completed_occurrence_id == first.next_occurrence_id
    assert clock.value < current.next_due_at <= clock.value + timedelta(seconds=60)
    assert calls == [task_id]
    _fire(host, task_id)
    assert calls == [task_id]
    host._start_task = _complete(store, calls)
    clock.value = current.next_due_at
    _fire(host, task_id)
    assert calls == [task_id, task_id]
    assert host.get(task_id).status is RecurrenceStatus.COMPLETED


def test_pause_survives_restart_and_explicit_resume_preserves_next_occurrence(tmp_path) -> None:
    store, clock, calls = _store(tmp_path), Clock(), []
    task_id = _ready(store)
    host = _host(store, _complete(store, calls), clock)
    first = host.create(task_id=task_id, start_at=clock.value)
    paused = host.pause(task_id)
    assert paused.status is RecurrenceStatus.PAUSED
    recovered = _host(store, _complete(store, calls), clock)
    recovered.start()
    try:
        assert recovered._jobs.list_enabled() == ()
        resumed = recovered.resume(task_id)
        assert resumed.next_occurrence_id == first.next_occurrence_id
        _fire(recovered, task_id)
        assert calls == [task_id]
    finally:
        recovered.shutdown()


def test_cancel_is_terminal_and_cancels_only_timer_not_running_task(tmp_path) -> None:
    store, clock, calls = _store(tmp_path), Clock(), []
    task_id = _ready(store)
    host = _host(store, _complete(store, calls), clock)
    host.create(task_id=task_id, start_at=clock.value)
    TaskQueue(store).transition(task_id, TaskState.RUNNING)
    state = host.cancel(task_id)
    assert state.status is RecurrenceStatus.CANCELLED
    assert host.resume(task_id).status is RecurrenceStatus.CANCELLED
    assert TaskQueue(store).get(task_id).state is TaskState.RUNNING
    assert calls == []


@pytest.mark.parametrize("missing", [False, True])
def test_missing_or_cancelled_task_is_suppressed_without_callback(tmp_path, missing) -> None:
    store, clock = _store(tmp_path), Clock()
    task_id = _ready(store)
    host = _host(store, lambda _: pytest.fail("terminal task dispatch"), clock)
    host.create(task_id=task_id, start_at=clock.value)
    if missing:
        with store.connection() as conn:
            conn.execute("DELETE FROM task_events WHERE task_id = ?", (task_id,))
            conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
    else:
        TaskQueue(store).transition(task_id, TaskState.CANCELLED)
    job = host._jobs.list_enabled()[0]
    host._scheduler._dispatch(job.job_id, job)
    assert host.get(task_id).status is RecurrenceStatus.CANCELLED
    assert host._jobs.list_enabled() == ()


def test_deadline_prevents_dispatch_and_records_canonical_terminal_state(tmp_path) -> None:
    store, clock = _store(tmp_path), Clock()
    task_id = _ready(store)
    host = _host(store, lambda _: pytest.fail("deadline dispatch"), clock)
    deadline = clock.value + timedelta(minutes=2)
    host.create(task_id=task_id, start_at=clock.value, deadline_at=deadline)
    clock.value = deadline
    _fire(host, task_id)
    state = host.get(task_id)
    assert state.status is RecurrenceStatus.COMPLETED
    assert state.terminal_reason is RecurrenceTerminalReason.DEADLINE
    assert TaskQueue(store).get(task_id).state is TaskState.READY


def test_already_started_task_cannot_be_freshly_replayed_after_host_restart(tmp_path) -> None:
    store, clock = _store(tmp_path), Clock()
    task_id = _ready(store)
    host = _host(store, lambda _: pytest.fail("started task replay"), clock)
    host.create(task_id=task_id, start_at=clock.value)
    TaskQueue(store).transition(task_id, TaskState.RUNNING)
    TaskQueue(store).transition(task_id, TaskState.PAUSED)
    recovered = _host(store, lambda _: pytest.fail("started task replay"), clock)
    _fire(recovered, task_id)
    assert recovered.get(task_id).status is RecurrenceStatus.COMPLETED
    assert TaskQueue(store).get(task_id).state is TaskState.PAUSED


def test_durable_start_ack_does_not_wait_for_long_running_task_completion(tmp_path) -> None:
    store, clock, calls = _store(tmp_path), Clock(), []
    task_id = _ready(store)

    def start(key):
        calls.append(key)
        TaskQueue(store).transition(key, TaskState.RUNNING)
        return True

    host = _host(store, start, clock)
    host.create(task_id=task_id, start_at=clock.value)
    _fire(host, task_id)
    assert calls == [task_id]
    assert host.get(task_id).status is RecurrenceStatus.COMPLETED
    assert TaskQueue(store).get(task_id).state is TaskState.RUNNING
    host.shutdown()


def test_cancel_committed_during_admission_is_not_undone_by_completion(tmp_path) -> None:
    store, clock = _store(tmp_path), Clock()
    task_id = _ready(store)

    def start(key):
        TaskQueue(store).transition(key, TaskState.RUNNING)
        host.cancel(key)
        return True

    host = _host(store, start, clock)
    host.create(task_id=task_id, start_at=clock.value)
    _fire(host, task_id)
    assert host.get(task_id).status is RecurrenceStatus.CANCELLED
    assert host._jobs.list_enabled() == ()
    assert TaskQueue(store).get(task_id).state is TaskState.RUNNING


def test_pause_committed_during_resource_wait_keeps_progress_disabled(tmp_path) -> None:
    store, clock = _store(tmp_path), Clock()
    task_id = _ready(store)

    def wait(key):
        host.pause(key)
        return False

    host = _host(store, wait, clock)
    initial = host.create(task_id=task_id, start_at=clock.value)
    _fire(host, task_id)
    current = host.get(task_id)
    assert current.status is RecurrenceStatus.PAUSED
    assert current.last_completed_occurrence_id == initial.next_occurrence_id
    assert host._jobs.list_enabled() == ()


def test_unknown_scheduler_action_has_no_runtime_callback(tmp_path) -> None:
    store, clock = _store(tmp_path), Clock()
    task_id = _ready(store)
    host = _host(store, lambda _: pytest.fail("unknown action dispatch"), clock)
    job = ScheduledJob(
        job_id="foreign-action", action_id="foreign.action", trigger_kind=TriggerKind.DATE,
        trigger={"run_date": clock.value.isoformat()}, payload={"task_id": task_id},
    )
    host._jobs.upsert(job)
    with pytest.raises(ValueError, match="Невідомий"):
        host._scheduler._dispatch(job.job_id, job)


@pytest.mark.parametrize("result", [None, 0, 1, "true", {}])
def test_non_bool_admission_is_rejected_without_advancing_intent(tmp_path, result) -> None:
    store, clock = _store(tmp_path), Clock()
    task_id = _ready(store)
    host = _host(store, lambda _: result, clock)
    initial = host.create(task_id=task_id, start_at=clock.value)
    with pytest.raises(TypeError, match="непідтверджений"):
        _fire(host, task_id)
    assert host.get(task_id) == initial


def test_early_true_ack_is_rejected_without_losing_ready_task_intent(tmp_path) -> None:
    store, clock = _store(tmp_path), Clock()
    task_id = _ready(store)
    host = _host(store, lambda _: True, clock)
    initial = host.create(task_id=task_id, start_at=clock.value)
    with pytest.raises(RuntimeError, match="не підтверджено"):
        _fire(host, task_id)
    assert host.get(task_id) == initial
    assert TaskQueue(store).get(task_id).state is TaskState.READY


def test_port_failure_preserves_exact_occurrence_for_reconciliation(tmp_path) -> None:
    store, clock = _store(tmp_path), Clock()
    task_id = _ready(store)

    def fail(_key):
        raise RuntimeError("synthetic uncertain effect")

    host = _host(store, fail, clock)
    initial = host.create(task_id=task_id, start_at=clock.value)
    with pytest.raises(RuntimeError, match="uncertain"):
        _fire(host, task_id)
    assert host.get(task_id) == initial


@pytest.mark.parametrize("field", ["workspace_id", "agent_id"])
def test_foreign_task_cannot_be_scheduled_or_reassigned_at_dispatch(tmp_path, field) -> None:
    store, clock = _store(tmp_path), Clock()
    host = _host(store, lambda _: pytest.fail("foreign task dispatch"), clock)
    foreign = _ready(store, **{field: "foreign"})
    with pytest.raises(ValueError, match="іншому"):
        host.create(task_id=foreign, start_at=clock.value)
    task_id = _ready(store)
    host.create(task_id=task_id, start_at=clock.value)
    with store.connection() as conn:
        conn.execute(f"UPDATE tasks SET {field} = ? WHERE task_id = ?", ("foreign", task_id))
    with pytest.raises(ValueError, match="іншому"):
        _fire(host, task_id)


@pytest.mark.parametrize("task_id", [None, True, 1, "", " task ", "not-a-uuid"])
def test_invalid_task_identity_never_creates_intent(tmp_path, task_id) -> None:
    store, clock = _store(tmp_path), Clock()
    host = _host(store, lambda _: pytest.fail("invalid identity dispatch"), clock)
    with pytest.raises((TypeError, ValueError)):
        host.create(task_id=task_id, start_at=clock.value)
    assert host._jobs.list_enabled() == ()


def test_forged_invocation_cannot_route_a_different_task(tmp_path) -> None:
    store, clock = _store(tmp_path), Clock()
    first, second = _ready(store), _ready(store)
    host = _host(store, lambda _: pytest.fail("forged invocation dispatch"), clock)
    state = host.create(task_id=first, start_at=clock.value)
    invocation = RecurrenceInvocation(
        recurrence_id=state.recurrence_id, occurrence_id=state.next_occurrence_id,
        scheduled_for=clock.value, payload={"task_id": second},
    )
    with pytest.raises(ValueError, match="Ідентичність"):
        host._dispatch_task(invocation)
    with pytest.raises(ValueError, match="прив'язування"):
        host._dispatch_task(replace(invocation, payload={"task_id": first, "action": "other"}))


def test_snapshot_is_bounded_text_state_without_command_or_callbacks(tmp_path) -> None:
    store, clock = _store(tmp_path), Clock()
    task_id = _ready(store)
    host = _host(store, lambda _: False, clock)
    state = host.create(task_id=task_id, start_at=clock.value)
    assert host.snapshot() == [{
        "task_id": task_id, "status": "active", "next_due_at": clock.value.isoformat(),
        "deadline_at": None, "retry_interval_seconds": state.interval_seconds,
    }]
    with pytest.raises(ValueError):
        host.snapshot(limit=True)


def test_unknown_inner_action_cannot_be_dispatched_by_canonical_recurrence(tmp_path) -> None:
    store, clock = _store(tmp_path), Clock()
    task_id = _ready(store)
    host = _host(store, lambda _: pytest.fail("unknown inner action"), clock)
    state = host._recurrence.create(
        recurrence_id="desktop-task:" + task_id, task_id=task_id,
        action_id="unregistered.action", interval_seconds=60,
        start_at=clock.value, payload={"task_id": task_id},
    )
    with pytest.raises(ValueError, match="Невідомий"):
        _fire(host, task_id)
    assert host.get(task_id) == state


def test_split_audit_store_is_rejected_before_host_construction(tmp_path) -> None:
    store = _store(tmp_path)
    another = SQLiteStore(tmp_path / "other.db")
    another.initialize()
    with pytest.raises(ValueError, match="спільне"):
        PackagedTaskScheduler(store=store, audit=AuditLog(another), start_task=lambda _: False)


def test_actual_apscheduler_worker_fires_one_ready_task(tmp_path) -> None:
    store = _store(tmp_path)
    task_id, calls, done = _ready(store), [], threading.Event()
    complete = _complete(store, calls)

    def start(key):
        result = complete(key)
        done.set()
        return result

    host = _host(store, start)
    host.create(task_id=task_id, start_at=datetime.now(UTC) - timedelta(seconds=1))
    host.start()
    try:
        assert done.wait(timeout=5), "real APScheduler worker did not dispatch"
    finally:
        host.shutdown()
    assert calls == [task_id]
    assert host.get(task_id).status is RecurrenceStatus.COMPLETED


def test_actual_failed_date_intent_can_be_explicitly_resumed_without_restart(tmp_path) -> None:
    store = _store(tmp_path)
    task_id, calls = _ready(store), []
    failed, completed = threading.Event(), threading.Event()

    def start(key):
        calls.append(key)
        if len(calls) == 1:
            raise RuntimeError("synthetic unknown admission")
        TaskQueue(store).transition(key, TaskState.RUNNING)
        return True

    host = _host(store, start)
    host._scheduler._scheduler.add_listener(lambda _: failed.set(), EVENT_JOB_ERROR)
    host._scheduler._scheduler.add_listener(lambda _: completed.set(), EVENT_JOB_EXECUTED)
    original = host.create(task_id=task_id, start_at=datetime.now(UTC) - timedelta(seconds=1))
    host.start()
    try:
        assert failed.wait(timeout=5), "actual failed DATE callback did not settle"
        assert host.get(task_id) == original
        assert host._scheduler._scheduler.get_jobs() == []
        host.resume(task_id)
        assert completed.wait(timeout=3), "explicit resume lost the durable DATE intent"
        assert host.get(task_id).last_completed_occurrence_id == original.next_occurrence_id
    finally:
        host.shutdown()
    assert calls == [task_id, task_id]
    assert TaskQueue(store).get(task_id).state is TaskState.RUNNING


def test_explicit_resume_after_late_start_never_calls_starter_again(tmp_path) -> None:
    store = _store(tmp_path)
    task_id, calls = _ready(store), []
    failed, completed = threading.Event(), threading.Event()

    def start(key):
        calls.append(key)
        raise RuntimeError("synthetic delayed admission acknowledgement")

    host = _host(store, start)
    host._scheduler._scheduler.add_listener(lambda _: failed.set(), EVENT_JOB_ERROR)
    host._scheduler._scheduler.add_listener(lambda _: completed.set(), EVENT_JOB_EXECUTED)
    initial = host.create(task_id=task_id, start_at=datetime.now(UTC) - timedelta(seconds=1))
    host.start()
    try:
        assert failed.wait(timeout=5)
        assert host.get(task_id) == initial
        # The existing desktop future can acknowledge a late durable start.
        TaskQueue(store).transition(task_id, TaskState.RUNNING)
        host.resume(task_id)
        assert completed.wait(timeout=3), "late-start intent did not reconcile"
        assert host.get(task_id).status is RecurrenceStatus.COMPLETED
    finally:
        host.shutdown()
    assert calls == [task_id]


@pytest.mark.parametrize("control", ["pause", "cancel"])
def test_explicit_reactivation_cannot_override_newer_timer_control(
    tmp_path, monkeypatch, control,
) -> None:
    store, clock = _store(tmp_path), Clock()
    task_id = _ready(store)
    host = _host(store, lambda _: pytest.fail("controlled timer dispatch"), clock)
    host.create(task_id=task_id, start_at=clock.value)
    host.start()
    try:
        job = host._jobs.list_enabled()[0]
        host._scheduler._scheduler.remove_job(job.job_id)
        activate = host._scheduler.activate_persisted

        def control_then_activate(candidate):
            monkeypatch.setattr(host._scheduler, "activate_persisted", activate)
            getattr(host, control)(task_id)
            activate(candidate)

        monkeypatch.setattr(host._scheduler, "activate_persisted", control_then_activate)
        result = host.resume(task_id)
        expected = RecurrenceStatus.PAUSED if control == "pause" else RecurrenceStatus.CANCELLED
        assert result.status is expected
        assert host.get(task_id).status is expected
        assert not host._scheduler.has_runtime_job(job.job_id)
    finally:
        host.shutdown()


def test_explicit_reactivation_preserves_new_recreated_definition(tmp_path, monkeypatch) -> None:
    store, clock = _store(tmp_path), Clock()
    task_id = _ready(store)
    host = _host(store, lambda _: pytest.fail("new definition early dispatch"), clock)
    original_state = host.create(task_id=task_id, start_at=clock.value)
    host.start()
    try:
        old_job = host._jobs.list_enabled()[0]
        host._scheduler._scheduler.remove_job(old_job.job_id)
        activate = host._scheduler.activate_persisted
        recreated = []

        def recreate_then_activate(candidate):
            monkeypatch.setattr(host._scheduler, "activate_persisted", activate)
            assert host._jobs.delete(old_job.job_id)
            recreated.append(host.create(
                task_id=task_id, start_at=clock.value + timedelta(minutes=10),
                retry_interval_seconds=300,
            ))
            activate(candidate)

        monkeypatch.setattr(host._scheduler, "activate_persisted", recreate_then_activate)
        current = host.resume(task_id)
        assert current == recreated[0]
        assert current.next_occurrence_id != original_state.next_occurrence_id
        assert current.interval_seconds == 300
        stored = host._jobs.get(old_job.job_id)
        runtime = host._scheduler._scheduler.get_job(old_job.job_id)
        assert runtime.args == (old_job.job_id, stored)
        assert runtime.args[1] != old_job
    finally:
        host.shutdown()


def test_schedule_is_not_hidden_by_fifty_newer_unscheduled_tasks(tmp_path) -> None:
    store, clock = _store(tmp_path), Clock()
    task_id = _ready(store)
    host = _host(store, lambda _: pytest.fail("early schedule dispatch"), clock)
    host.create(task_id=task_id, start_at=clock.value)
    for _ in range(51):
        _ready(store)
    assert [item["task_id"] for item in host.snapshot()] == [task_id]


def test_more_than_fifty_schedules_remain_pageable_and_controllable(tmp_path) -> None:
    store, clock = _store(tmp_path), Clock()
    host = _host(store, lambda _: pytest.fail("paged schedule early dispatch"), clock)
    task_ids = []
    for _ in range(51):
        key = _ready(store)
        host.create(task_id=key, start_at=clock.value)
        task_ids.append(key)
    first = host.snapshot_page()
    assert len(first["items"]) == 50
    assert first["has_more"] is True
    second = host.snapshot_page(after_job_id=first["next_after_job_id"])
    assert len(second["items"]) == 1
    assert second["has_more"] is False
    assert second["next_after_job_id"] is None
    actual = [item["task_id"] for item in [*first["items"], *second["items"]]]
    assert len(actual) == len(set(actual)) == 51
    assert set(actual) == set(task_ids)
    last_key = second["items"][0]["task_id"]
    assert host.pause(last_key).status is RecurrenceStatus.PAUSED
    assert host.resume(last_key).status is RecurrenceStatus.ACTIVE
    assert host.cancel(last_key).status is RecurrenceStatus.CANCELLED

    # The extra candidate identifies a next page; its payload must not be hydrated
    # while the first page is read. Corruption is still rejected on its own page.
    with store.connection() as conn:
        last_job_id = conn.execute(
            "SELECT job_id FROM scheduled_jobs ORDER BY job_id DESC LIMIT 1"
        ).fetchone()["job_id"]
        conn.execute(
            "UPDATE scheduled_jobs SET payload_json = ? WHERE job_id = ?",
            ("not-json", last_job_id),
        )
    assert len(host.snapshot_page()["items"]) == 50
    with pytest.raises(ValueError, match="corrupt"):
        host.snapshot_page(after_job_id=first["next_after_job_id"])


def test_foreign_empty_page_still_advances_to_the_ordinary_schedule(tmp_path) -> None:
    store, clock = _store(tmp_path), Clock()
    host = _host(store, lambda _: pytest.fail("foreign page dispatch"), clock)
    key = _ready(store)
    host.create(task_id=key, start_at=clock.value)
    foreign_id = "recurrence-v1:" + "0" * 64
    host._jobs.upsert(ScheduledJob(
        job_id=foreign_id, action_id="scheduler.recurrence.dispatch", trigger_kind=TriggerKind.DATE,
        trigger={"run_date": clock.value.isoformat()}, payload={"recurrence_id": "foreign:other"},
    ))
    page = host.snapshot_page(limit=1)
    assert page == {"items": [], "next_after_job_id": foreign_id, "has_more": True}
    following = host.snapshot_page(limit=1, after_job_id=page["next_after_job_id"])
    assert [item["task_id"] for item in following["items"]] == [key]
    assert following["has_more"] is False


@pytest.mark.parametrize("cursor", [True, 1, "", "wrong", "recurrence-v1:" + "A" * 64])
def test_malformed_page_cursor_is_not_an_execution_capability(tmp_path, cursor) -> None:
    store = _store(tmp_path)
    host = _host(store, lambda _: pytest.fail("cursor dispatch"))
    with pytest.raises(ValueError, match="покажчик"):
        host.snapshot_page(after_job_id=cursor)
