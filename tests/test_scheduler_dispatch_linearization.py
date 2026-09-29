from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.scheduler import (
    APSchedulerAdapter,
    ScheduledJob,
    ScheduledJobStore,
    TriggerKind,
)


def _sqlite(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "scheduler-linearization.sqlite3")
    store.initialize()
    return store


def _job(*, payload: dict[str, object] | None = None) -> ScheduledJob:
    run_at = datetime.now(UTC) + timedelta(days=1)
    return ScheduledJob(
        job_id="linearized-job",
        action_id="linearized.action",
        trigger_kind=TriggerKind.DATE,
        trigger={"run_date": run_at.isoformat()},
        payload={} if payload is None else payload,
        misfire_grace_seconds=3600,
    )


def test_dispatch_authorization_requires_exact_enabled_snapshot(tmp_path: Path) -> None:
    jobs = ScheduledJobStore(_sqlite(tmp_path))
    installed = _job(payload={"generation": "old"})
    jobs.upsert(installed)

    authorized = jobs.authorize_dispatch(installed)

    assert authorized == installed
    replacement = replace(installed, payload={"generation": "new"})
    jobs.upsert(replacement)
    assert jobs.authorize_dispatch(installed) is None
    assert jobs.authorize_dispatch(replacement) == replacement

    jobs.set_enabled(replacement.job_id, False)
    assert jobs.authorize_dispatch(replace(replacement, enabled=False)) is None


def test_dispatch_authorization_binds_task_state_in_same_authority(tmp_path: Path) -> None:
    sqlite = _sqlite(tmp_path)
    queue = TaskQueue(sqlite)
    jobs = ScheduledJobStore(sqlite)
    task = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "scheduled linearization"},
    )
    queue.transition(task.task_id, TaskState.READY)
    queue.transition(task.task_id, TaskState.RUNNING)
    installed = _job(payload={"task_id": task.task_id})
    jobs.upsert(installed)

    assert jobs.authorize_dispatch(installed) == installed

    queue.transition(task.task_id, TaskState.CANCELLED)

    assert jobs.authorize_dispatch(installed) is None


def test_replacement_before_authorization_blocks_old_occurrence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs = ScheduledJobStore(_sqlite(tmp_path))
    installed = _job(payload={"generation": "old"})
    replacement = replace(installed, payload={"generation": "new"})
    jobs.upsert(installed)
    original_authorize = jobs.authorize_dispatch
    resolver_calls: list[str] = []
    calls: list[dict[str, object]] = []

    def replace_before_authorize(expected: ScheduledJob) -> ScheduledJob | None:
        jobs.upsert(replacement)
        return original_authorize(expected)

    def resolve(action_id: str):
        resolver_calls.append(action_id)
        return lambda payload: calls.append(payload)

    monkeypatch.setattr(jobs, "authorize_dispatch", replace_before_authorize)
    adapter = APSchedulerAdapter(jobs, resolve)

    adapter._dispatch(installed.job_id, installed)

    assert resolver_calls == []
    assert calls == []
    assert jobs.get(installed.job_id) == replacement


def test_disabled_before_authorization_never_resolves_handler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs = ScheduledJobStore(_sqlite(tmp_path))
    installed = _job(payload={"generation": "disabled"})
    jobs.upsert(installed)
    original_authorize = jobs.authorize_dispatch
    resolver_calls: list[str] = []

    def disable_before_authorize(expected: ScheduledJob) -> ScheduledJob | None:
        jobs.set_enabled(expected.job_id, False)
        return original_authorize(expected)

    monkeypatch.setattr(jobs, "authorize_dispatch", disable_before_authorize)
    adapter = APSchedulerAdapter(
        jobs,
        lambda action_id: resolver_calls.append(action_id) or (lambda _payload: None),
    )

    adapter._dispatch(installed.job_id, installed)

    assert resolver_calls == []
    persisted = jobs.get(installed.job_id)
    assert persisted is not None
    assert persisted.enabled is False


def test_terminal_task_before_authorization_never_resolves_handler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sqlite = _sqlite(tmp_path)
    queue = TaskQueue(sqlite)
    jobs = ScheduledJobStore(sqlite)
    task = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "terminal-before-authorize"},
    )
    queue.transition(task.task_id, TaskState.READY)
    queue.transition(task.task_id, TaskState.RUNNING)
    installed = _job(payload={"task_id": task.task_id})
    jobs.upsert(installed)
    original_authorize = jobs.authorize_dispatch
    resolver_calls: list[str] = []

    def terminalize_before_authorize(expected: ScheduledJob) -> ScheduledJob | None:
        queue.transition(task.task_id, TaskState.CANCELLED)
        return original_authorize(expected)

    monkeypatch.setattr(jobs, "authorize_dispatch", terminalize_before_authorize)
    adapter = APSchedulerAdapter(
        jobs,
        lambda action_id: resolver_calls.append(action_id) or (lambda _payload: None),
    )

    adapter._dispatch(installed.job_id, installed)

    assert resolver_calls == []
    assert queue.get(task.task_id).state is TaskState.CANCELLED


def test_authorization_before_pause_preserves_only_claimed_occurrence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    jobs = ScheduledJobStore(_sqlite(tmp_path))
    installed = _job(payload={"generation": "claimed"})
    jobs.upsert(installed)
    original_authorize = jobs.authorize_dispatch
    calls: list[dict[str, object]] = []

    def authorize_before_pause(expected: ScheduledJob) -> ScheduledJob | None:
        authorized = original_authorize(expected)
        assert authorized is not None
        jobs.set_enabled(expected.job_id, False)
        return authorized

    monkeypatch.setattr(jobs, "authorize_dispatch", authorize_before_pause)
    adapter = APSchedulerAdapter(
        jobs,
        lambda _action_id: lambda payload: calls.append(payload),
    )

    adapter._dispatch(installed.job_id, installed)
    adapter._dispatch(installed.job_id, installed)

    assert calls == [{"generation": "claimed"}]
    persisted = jobs.get(installed.job_id)
    assert persisted is not None
    assert persisted.enabled is False


def test_compare_disable_rejects_stale_snapshot(tmp_path: Path) -> None:
    jobs = ScheduledJobStore(_sqlite(tmp_path))
    stale = _job(payload={"generation": "old"})
    replacement = replace(stale, payload={"generation": "new"})
    jobs.upsert(stale)
    jobs.upsert(replacement)

    assert jobs.disable_if_current(stale) is False

    current = jobs.get(stale.job_id)
    assert current == replacement
    assert current.enabled is True


def test_runtime_sync_preserves_replacement_when_stale_suppression_loses_cas(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sqlite = _sqlite(tmp_path)
    queue = TaskQueue(sqlite)
    jobs = ScheduledJobStore(sqlite)
    task = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "stale suppression race"},
    )
    queue.transition(task.task_id, TaskState.READY)
    queue.transition(task.task_id, TaskState.RUNNING)
    stale = _job(payload={"task_id": task.task_id, "generation": "old"})
    replacement = replace(stale, payload={"generation": "new"})
    jobs.upsert(stale)
    adapter = APSchedulerAdapter(jobs, lambda _action_id: lambda _payload: None)
    adapter.start()
    queue.transition(task.task_id, TaskState.CANCELLED)

    original_task_state = jobs.task_state
    replaced = False

    def replace_during_task_check(task_id: str) -> TaskState | None:
        nonlocal replaced
        if not replaced:
            replaced = True
            jobs.upsert(replacement)
        return original_task_state(task_id)

    monkeypatch.setattr(jobs, "task_state", replace_during_task_check)

    synced = adapter._sync_runtime_job(stale.job_id)

    current = jobs.get(stale.job_id)
    runtime = adapter._scheduler.get_job(stale.job_id)
    assert synced == replacement
    assert current == replacement
    assert current.enabled is True
    assert runtime is not None
    assert tuple(runtime.args)[1] == replacement
    adapter.shutdown(wait=False)


def test_runtime_sync_bounds_continuous_stale_suppression_churn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sqlite = _sqlite(tmp_path)
    queue = TaskQueue(sqlite)
    jobs = ScheduledJobStore(sqlite)
    task = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "bounded scheduler churn"},
    )
    queue.transition(task.task_id, TaskState.READY)
    queue.transition(task.task_id, TaskState.RUNNING)
    initial = _job(payload={"task_id": task.task_id, "generation": 0})
    jobs.upsert(initial)
    adapter = APSchedulerAdapter(jobs, lambda _action_id: lambda _payload: None)
    adapter.start()
    queue.transition(task.task_id, TaskState.CANCELLED)
    churn_count = 0

    def replace_on_every_task_check(task_id: str) -> TaskState | None:
        nonlocal churn_count
        churn_count += 1
        jobs.upsert(
            replace(
                initial,
                payload={"task_id": task_id, "generation": churn_count},
            )
        )
        return TaskState.CANCELLED

    monkeypatch.setattr(jobs, "task_state", replace_on_every_task_check)

    assert adapter._sync_runtime_job(initial.job_id) is None

    current = jobs.get(initial.job_id)
    assert churn_count == 3
    assert current is not None
    assert current.payload["generation"] == churn_count
    assert current.enabled is True
    assert not adapter.has_runtime_job(initial.job_id)
    adapter.shutdown(wait=False)



def test_runtime_sync_serializes_concurrent_replacement_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replacement_committed = Event()

    class SignalingJobs(ScheduledJobStore):
        def upsert(self, job: ScheduledJob) -> None:
            super().upsert(job)
            if job.payload.get("generation") == "new":
                replacement_committed.set()

    jobs = SignalingJobs(_sqlite(tmp_path))
    adapter = APSchedulerAdapter(jobs, lambda _action_id: lambda _payload: None)
    adapter.start()
    stale = _job(payload={"generation": "old"})
    replacement = replace(
        stale,
        trigger={
            "run_date": (datetime.now(UTC) + timedelta(days=2)).isoformat(),
        },
        payload={"generation": "new"},
    )
    stale_install_entered = Event()
    release_stale_install = Event()
    original_install = adapter._install

    def blocking_install(job: ScheduledJob) -> None:
        if job == stale and not stale_install_entered.is_set():
            stale_install_entered.set()
            if not release_stale_install.wait(timeout=5):
                raise AssertionError("stale install was not released")
        original_install(job)

    monkeypatch.setattr(adapter, "_install", blocking_install)
    with ThreadPoolExecutor(max_workers=2) as executor:
        stale_future = executor.submit(adapter.upsert, stale)
        assert stale_install_entered.wait(timeout=5)
        replacement_future = executor.submit(adapter.upsert, replacement)
        assert replacement_committed.wait(timeout=5)
        release_stale_install.set()
        stale_future.result(timeout=5)
        replacement_future.result(timeout=5)
    durable = jobs.get(stale.job_id)
    runtime = adapter._scheduler.get_job(stale.job_id)
    assert durable == replacement
    assert runtime is not None
    assert tuple(runtime.args)[1] == replacement
    adapter.shutdown(wait=False)


def test_dispatch_suppression_cannot_remove_replacement_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sqlite = _sqlite(tmp_path)
    queue = TaskQueue(sqlite)
    jobs = ScheduledJobStore(sqlite)
    task = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "suppression runtime replacement"},
    )
    queue.transition(task.task_id, TaskState.READY)
    queue.transition(task.task_id, TaskState.RUNNING)
    stale = _job(payload={"task_id": task.task_id, "generation": "old"})
    replacement = replace(stale, payload={"generation": "new"})
    jobs.upsert(stale)
    handled: list[dict[str, object]] = []
    adapter = APSchedulerAdapter(
        jobs,
        lambda _action_id: lambda payload: handled.append(payload),
    )
    adapter.start()
    old_runtime = adapter._scheduler.get_job(stale.job_id)
    assert old_runtime is not None
    old_args = tuple(old_runtime.args)
    queue.transition(task.task_id, TaskState.CANCELLED)
    original_disable = jobs.disable_if_current
    replaced = False

    def disable_then_replace(expected: ScheduledJob) -> bool:
        nonlocal replaced
        disabled = original_disable(expected)
        if disabled and not replaced:
            replaced = True
            adapter.upsert(replacement)
        return disabled

    monkeypatch.setattr(jobs, "disable_if_current", disable_then_replace)

    adapter._dispatch(*old_args)

    current = jobs.get(stale.job_id)
    runtime = adapter._scheduler.get_job(stale.job_id)
    assert handled == []
    assert current == replacement
    assert current.enabled is True
    assert runtime is not None
    assert tuple(runtime.args)[1] == replacement
    adapter.shutdown(wait=False)
