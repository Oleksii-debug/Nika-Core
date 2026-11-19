from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Event

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.scheduler import APSchedulerAdapter, ScheduledJobStore
from nika_core.scheduler.recurrence import DurableRecurrenceService, RecurrenceStatus
from nika_core.scheduler.store import IMMUTABLE_JOB_BINDING_KEY


def _pair(tmp_path: Path):
    path = tmp_path / "Ніка lifecycle CAS" / "core.db"
    first_db = SQLiteStore(path)
    first_db.initialize()
    queue = TaskQueue(first_db)
    task = queue.create(workspace_id="default", agent_id="test", payload={})
    queue.transition(task.task_id, TaskState.READY)
    queue.transition(task.task_id, TaskState.RUNNING)
    effects: list[str] = []
    now = datetime(2035, 1, 1, tzinfo=UTC)
    services = []
    stores = []
    for db in (first_db, SQLiteStore(path)):
        jobs = ScheduledJobStore(db)
        adapter = APSchedulerAdapter(jobs, lambda _: lambda payload: None)
        service = DurableRecurrenceService(
            jobs=jobs, scheduler=adapter, clock=lambda: now,
            handler_resolver=lambda _: lambda invocation: effects.append(invocation.occurrence_id),
        )
        services.append(service)
        stores.append(jobs)
    services[0].create(
        recurrence_id="race", task_id=task.task_id, action_id="test.effect",
        interval_seconds=60, start_at=now,
    )
    return services, stores, effects


@pytest.mark.parametrize("winner", ["cancel", "pause"])
def test_stale_finish_cannot_reenable_concurrent_lifecycle_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, winner: str
) -> None:
    (worker, controller), stores, effects = _pair(tmp_path)
    entered, release = Event(), Event()
    original_persist = worker._persist

    def delayed_finish(state, payload, **kwargs):
        if state.status is RecurrenceStatus.ACTIVE and state.last_completed_occurrence_id:
            entered.set()
            assert release.wait(timeout=5)
        return original_persist(state, payload, **kwargs)

    monkeypatch.setattr(worker, "_persist", delayed_finish)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(worker.action_handler, {"recurrence_id": "race"})
        try:
            assert entered.wait(timeout=5)
            committed = getattr(controller, winner)("race")
            assert controller.get("race") == committed
        finally:
            release.set()
        pending.result(timeout=5)

    final = controller.get("race")
    expected = RecurrenceStatus.CANCELLED if winner == "cancel" else RecurrenceStatus.PAUSED
    assert final is not None and final.status is expected
    if winner == "cancel":
        assert final.next_due_at is None and final.next_occurrence_id is None
    assert all(not jobs.list_enabled() for jobs in stores)
    controller.action_handler({"recurrence_id": "race"})
    assert len(effects) == 1


@pytest.mark.parametrize("stale_command", ["pause", "resume"])
def test_stale_pause_or_resume_cannot_overwrite_concurrent_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stale_command: str
) -> None:
    (worker, controller), stores, effects = _pair(tmp_path)
    if stale_command == "resume":
        controller.pause("race")
    entered, release = Event(), Event()
    original_persist = worker._persist

    def delayed_transition(state, payload, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original_persist(state, payload, **kwargs)

    monkeypatch.setattr(worker, "_persist", delayed_transition)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(getattr(worker, stale_command), "race")
        try:
            assert entered.wait(timeout=5)
            controller.cancel("race")
        finally:
            release.set()
        pending.result(timeout=5)
    final = controller.get("race")
    assert final is not None and final.status is RecurrenceStatus.CANCELLED
    assert final.next_occurrence_id is None
    assert all(not jobs.list_enabled() for jobs in stores)
    assert effects == []



def test_delayed_cancel_retries_against_advanced_cursor_without_losing_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (worker, controller), stores, effects = _pair(tmp_path)
    entered, release = Event(), Event()
    original_persist = worker._persist

    def delayed_cancel(state, payload, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original_persist(state, payload, **kwargs)

    monkeypatch.setattr(worker, "_persist", delayed_cancel)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(worker.cancel, "race")
        try:
            assert entered.wait(timeout=5)
            controller.action_handler({"recurrence_id": "race"})
            advanced = controller.get("race")
            assert advanced is not None and advanced.last_completed_occurrence_id
        finally:
            release.set()
        cancelled = pending.result(timeout=5)
    assert cancelled.status is RecurrenceStatus.CANCELLED
    assert cancelled.last_completed_occurrence_id == advanced.last_completed_occurrence_id
    assert len(effects) == 1
    assert all(not jobs.list_enabled() for jobs in stores)


def test_concurrent_same_definition_create_cannot_resurrect_cancelled_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (worker, controller), stores, effects = _pair(tmp_path)
    definition = controller.get("race")
    assert definition is not None
    job = stores[0].list_enabled()[0]
    stores[1].delete(job.job_id)
    entered, release = Event(), Event()
    original_persist = worker._persist

    def delayed_create(state, payload, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original_persist(state, payload, **kwargs)

    monkeypatch.setattr(worker, "_persist", delayed_create)
    args = {
        "recurrence_id": "race", "task_id": definition.task_id,
        "action_id": definition.action_id, "interval_seconds": definition.interval_seconds,
        "start_at": definition.anchor_at,
    }
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(worker.create, **args)
        try:
            assert entered.wait(timeout=5)
            controller.create(**args)
            controller.cancel("race")
        finally:
            release.set()
        result = pending.result(timeout=5)
    assert result.status is RecurrenceStatus.CANCELLED
    assert all(not jobs.list_enabled() for jobs in stores)
    assert effects == []


def test_stale_completion_cannot_advance_recreated_definition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (worker, controller), stores, effects = _pair(tmp_path)
    definition = controller.get("race")
    assert definition is not None
    old_job = stores[0].list_enabled()[0]
    entered, release = Event(), Event()
    original_persist = worker._persist

    def delayed_finish(state, payload, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        return original_persist(state, payload, **kwargs)

    monkeypatch.setattr(worker, "_persist", delayed_finish)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(worker.action_handler, {"recurrence_id": "race"})
        try:
            assert entered.wait(timeout=5)
            assert stores[1].delete(old_job.job_id)
            successor = controller.create(
                recurrence_id="race", task_id=definition.task_id, action_id="test.successor",
                interval_seconds=120, start_at=definition.anchor_at + timedelta(minutes=1),
                payload={"typed": 1},
            )
        finally:
            release.set()
        with pytest.raises(ValueError, match="different recurrence"):
            pending.result(timeout=5)
    assert controller.get("race") == successor
    controller.action_handler({"recurrence_id": "race"})
    assert len(effects) == 1
    assert stores[0].get(old_job.job_id).payload["target_payload"] == {"typed": 1}


def test_lifecycle_cas_churn_is_bounded_and_does_not_activate_uncommitted_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (worker, controller), stores, effects = _pair(tmp_path)
    current = controller.get("race")
    attempts = []

    def conflict(*args, **kwargs):
        attempts.append((args, kwargs))
        return False

    monkeypatch.setattr(stores[0], "replace_if_current", conflict)
    with pytest.raises(RuntimeError, match="changed concurrently"):
        worker.pause("race")
    assert len(attempts) == 3
    assert controller.get("race") == current
    assert effects == []


def test_recurrence_rejects_unsupported_activation_before_any_cas_write(tmp_path: Path) -> None:
    class OldScheduler:
        def upsert(self, job):
            raise AssertionError("unsafe recurrence upsert fallback")

    (worker, controller), _stores, effects = _pair(tmp_path)
    worker._scheduler = OldScheduler()
    current = controller.get("race")
    with pytest.raises(TypeError, match="persisted-only activation"):
        worker.pause("race")
    assert controller.get("race") == current
    assert effects == []


@pytest.mark.parametrize("old_value,new_value", [(True, 1), (1, 1.0)])
def test_store_cas_rejects_python_equal_type_changed_snapshot(
    tmp_path: Path, old_value, new_value
) -> None:
    _, stores, _ = _pair(tmp_path)
    original = stores[0].list_enabled()[0]
    old = replace(original, payload={"typed": old_value})
    new = replace(old, payload={"typed": new_value})
    # Use an independent generic job, without altering the recurrence binding.
    old = replace(old, job_id="typed-cas")
    new = replace(new, job_id="typed-cas")
    stores[0].upsert(old)
    stores[1].upsert(new)
    assert not stores[0].replace_if_current(old, replace(old, enabled=False))
    current = stores[0].get(old.job_id)
    assert current is not None and current.enabled
    assert type(current.payload["typed"]) is type(new_value)


def test_store_cas_preserves_immutable_binding_and_expected_absence(tmp_path: Path) -> None:
    _, stores, _ = _pair(tmp_path)
    current = stores[0].list_enabled()[0]
    assert not stores[1].replace_if_current(None, replace(current, enabled=False))
    forged = replace(current, payload={**current.payload, IMMUTABLE_JOB_BINDING_KEY: "other"})
    with pytest.raises(ValueError, match="immutable binding conflict"):
        stores[1].replace_if_current(current, forged)
    assert stores[0].get(current.job_id) == current



def test_competing_store_cas_writers_have_exactly_one_winner(tmp_path: Path) -> None:
    _, stores, _ = _pair(tmp_path)
    template = stores[0].list_enabled()[0]
    initial = replace(template, job_id="atomic-cas", payload={"winner": "initial"})
    stores[0].upsert(initial)
    barrier = Barrier(2)

    def compete(index: int) -> bool:
        candidate = replace(initial, payload={"winner": index})
        barrier.wait(timeout=5)
        return stores[index].replace_if_current(initial, candidate)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(compete, index) for index in range(2)]
        results = [future.result(timeout=5) for future in futures]
    assert sorted(results) == [False, True]
    current = stores[0].get(initial.job_id)
    assert current is not None and current.payload["winner"] == results.index(True)


def test_recovery_rechecks_missing_task_authority_inside_cas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (worker, controller), stores, _ = _pair(tmp_path)
    template = controller.get("race")
    assert template is not None
    worker.create(
        recurrence_id="missing", task_id="appearing-task", action_id="test.effect",
        interval_seconds=60, start_at=template.anchor_at,
    )
    missing_job = next(job for job in stores[0].list_enabled()
                       if job.payload["recurrence_id"] == "missing")
    stores[0].set_enabled(missing_job.job_id, False)
    original_cas = stores[0].replace_if_current
    attempts = []

    def create_task_before_cas(expected, incoming, **kwargs):
        assert kwargs["require_terminal_task"] is True
        attempts.append(incoming)
        with stores[1]._store.connection() as conn:
            conn.execute(
                "INSERT INTO tasks(task_id, workspace_id, agent_id, state, payload_json, "
                "created_at, updated_at) SELECT ?, workspace_id, agent_id, state, "
                "payload_json, created_at, updated_at FROM tasks WHERE task_id = ?",
                ("appearing-task", template.task_id),
            )
        return original_cas(expected, incoming, **kwargs)

    monkeypatch.setattr(stores[0], "replace_if_current", create_task_before_cas)
    with pytest.raises(ValueError, match="enabled state does not match lifecycle"):
        worker.get("missing")
    assert len(attempts) == 1
    durable = stores[1].get(missing_job.job_id)
    assert durable is not None and not durable.enabled
    assert durable.payload["_nika_recurrence_v1"]["status"] == "active"



def test_recovery_can_commit_on_last_bounded_cas_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (worker, controller), stores, _ = _pair(tmp_path)
    current = controller.get("race")
    assert current is not None
    job = stores[0].list_enabled()[0]
    TaskQueue(stores[1]._store).transition(current.task_id, TaskState.CANCELLED)
    stores[1].set_enabled(job.job_id, False)
    original_cas = stores[0].replace_if_current
    attempts = []

    def conflict_twice(expected, incoming, **kwargs):
        attempts.append(incoming)
        if len(attempts) < 3:
            return False
        return original_cas(expected, incoming, **kwargs)

    monkeypatch.setattr(stores[0], "replace_if_current", conflict_twice)
    recovered = worker.get("race")
    assert recovered is not None and recovered.status is RecurrenceStatus.CANCELLED
    assert len(attempts) == 3
    assert controller.get("race") == recovered



def test_activation_after_cas_cannot_rewrite_a_newer_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (worker, controller), stores, effects = _pair(tmp_path)
    adapter = worker._scheduler
    original_activate = adapter.activate_persisted

    def cancel_before_activation(snapshot):
        # A separate store can write here: the CAS writer transaction is closed.
        controller.cancel("race")
        original_activate(snapshot)

    monkeypatch.setattr(adapter, "activate_persisted", cancel_before_activation)
    worker.action_handler({"recurrence_id": "race"})
    final = controller.get("race")
    assert final is not None and final.status is RecurrenceStatus.CANCELLED
    assert all(not jobs.list_enabled() for jobs in stores)
    controller.action_handler({"recurrence_id": "race"})
    assert len(effects) == 1


def test_activation_failure_after_committed_finish_does_not_replay_effect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (worker, controller), stores, effects = _pair(tmp_path)
    adapter = worker._scheduler

    def failed_activation(snapshot):
        raise RuntimeError("injected post-CAS runtime activation failure")

    monkeypatch.setattr(adapter, "activate_persisted", failed_activation)
    with pytest.raises(RuntimeError, match="post-CAS runtime activation failure"):
        worker.action_handler({"recurrence_id": "race"})
    committed = controller.get("race")
    assert committed is not None and committed.last_completed_occurrence_id == effects[0]
    controller.action_handler({"recurrence_id": "race"})
    assert len(effects) == 1
    adapter.start()
    try:
        job = stores[1].list_enabled()[0]
        runtime = adapter._scheduler.get_job(job.job_id)
        assert runtime is not None and runtime.args == (job.job_id, job)
    finally:
        adapter.shutdown(wait=False)



def test_resolver_recreated_definition_cannot_receive_the_old_handler_effect(tmp_path: Path) -> None:
    (worker, controller), stores, effects = _pair(tmp_path)
    original = controller.get("race")
    assert original is not None
    job = stores[0].list_enabled()[0]

    def replace_during_resolution(action_id):
        assert stores[1].delete(job.job_id)
        controller.create(
            recurrence_id="race", task_id=original.task_id, action_id="test.new-handler",
            interval_seconds=60, start_at=original.anchor_at, payload={"new": True},
        )
        return lambda invocation: effects.append("old-handler-with-new-definition")

    worker._handler_resolver = replace_during_resolution
    worker.action_handler({"recurrence_id": "race"})
    assert effects == []
    current = controller.get("race")
    assert current is not None and current.action_id == "test.new-handler"
    assert current.last_completed_occurrence_id is None
