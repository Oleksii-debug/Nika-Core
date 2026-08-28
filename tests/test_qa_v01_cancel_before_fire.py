from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.runtime.coordinator import TaskRuntimeCoordinator
from nika_core.scheduler.apscheduler_adapter import APSchedulerAdapter
from nika_core.scheduler.contracts import ScheduledJob, TriggerKind
from nika_core.scheduler.store import ScheduledJobStore


@dataclass(slots=True)
class _FakeClock:
    now: datetime

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class _AcceptedCancelRuntime:
    runtime_id = "qa-cancel-before-fire"

    def __init__(self) -> None:
        self.cancel_calls = 0

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        del task_id, thread_id
        self.cancel_calls += 1
        return True


def _waiting_task(queue: TaskQueue, *, name: str) -> str:
    task = queue.create(
        workspace_id="qa-v01-w14",
        agent_id="agent",
        payload={"name": name},
    )
    queue.transition(task.task_id, TaskState.READY)
    queue.transition(task.task_id, TaskState.RUNNING)
    queue.transition(task.task_id, TaskState.WAITING_TOOL)
    return task.task_id


def _date_job(*, job_id: str, action_id: str, task_id: str, run_at: datetime) -> ScheduledJob:
    return ScheduledJob(
        job_id=job_id,
        action_id=action_id,
        trigger_kind=TriggerKind.DATE,
        trigger={"run_date": run_at.isoformat()},
        payload={"task_id": task_id},
        misfire_grace_seconds=3600,
    )


def _run_due_date_jobs(
    adapter: APSchedulerAdapter,
    jobs: ScheduledJobStore,
    *,
    now: datetime,
) -> None:
    for job in jobs.list_enabled():
        if job.trigger_kind is not TriggerKind.DATE:
            continue
        run_at = datetime.fromisoformat(str(job.trigger["run_date"]))
        if run_at.tzinfo is None:
            run_at = run_at.replace(tzinfo=UTC)
        if run_at <= now:
            adapter._dispatch(job.job_id)


def test_cancelled_future_operation_never_fires_after_restart(tmp_path) -> None:
    db_path = tmp_path / "Nika QA Відкладені задачі" / "nika.db"
    store = SQLiteStore(db_path)
    store.initialize()
    queue = TaskQueue(store)
    jobs = ScheduledJobStore(store)

    cancelled_task_id = _waiting_task(queue, name="cancelled")
    unrelated_task_id = _waiting_task(queue, name="unrelated")

    clock = _FakeClock(datetime(2099, 1, 1, tzinfo=UTC))
    run_at = clock.now + timedelta(hours=1)
    cancelled_job_id = "qa-cancelled-future"
    unrelated_job_id = "qa-unrelated-future"

    jobs.upsert(
        _date_job(
            job_id=cancelled_job_id,
            action_id="cancelled",
            task_id=cancelled_task_id,
            run_at=run_at,
        )
    )
    jobs.upsert(
        _date_job(
            job_id=unrelated_job_id,
            action_id="unrelated",
            task_id=unrelated_task_id,
            run_at=run_at,
        )
    )

    calls = {"cancelled": 0, "unrelated": 0}

    def resolve(action_id: str):
        def handler(payload: dict[str, object]) -> None:
            assert payload["task_id"] in {cancelled_task_id, unrelated_task_id}
            calls[action_id] += 1

        return handler

    adapter = APSchedulerAdapter(jobs, resolve, audit=AuditLog(store))
    adapter.start()
    assert adapter.has_runtime_job(cancelled_job_id)
    assert adapter.has_runtime_job(unrelated_job_id)

    runtime = _AcceptedCancelRuntime()
    coordinator = TaskRuntimeCoordinator(queue, AuditLog(store))
    assert asyncio.run(
        coordinator.cancel(
            runtime,
            task_id=cancelled_task_id,
            thread_id="cancel-before-fire",
        )
    )
    assert asyncio.run(
        coordinator.cancel(
            runtime,
            task_id=cancelled_task_id,
            thread_id="cancel-before-fire",
        )
    )
    assert runtime.cancel_calls == 1
    assert queue.get(cancelled_task_id).state is TaskState.CANCELLED
    adapter.shutdown(wait=False)

    restarted_store = SQLiteStore(db_path)
    restarted_store.initialize()
    restarted_queue = TaskQueue(restarted_store)
    restarted_jobs = ScheduledJobStore(restarted_store)
    restarted_adapter = APSchedulerAdapter(restarted_jobs, resolve, audit=AuditLog(restarted_store))
    restarted_adapter.start()

    first_rehydrated_cancelled = restarted_adapter.has_runtime_job(cancelled_job_id)
    clock.advance(timedelta(hours=2))
    _run_due_date_jobs(restarted_adapter, restarted_jobs, now=clock.now)
    first_restart_calls = dict(calls)
    state_after_first_restart = restarted_queue.get(cancelled_task_id).state
    cancelled_row_after_first_restart = restarted_jobs.get(cancelled_job_id)
    cancelled_executable_after_first_restart = bool(
        cancelled_row_after_first_restart and cancelled_row_after_first_restart.enabled
    )
    restarted_adapter.shutdown(wait=False)

    repeated_store = SQLiteStore(db_path)
    repeated_store.initialize()
    repeated_queue = TaskQueue(repeated_store)
    repeated_jobs = ScheduledJobStore(repeated_store)
    repeated_adapter = APSchedulerAdapter(repeated_jobs, resolve, audit=AuditLog(repeated_store))
    repeated_adapter.start()

    repeated_rehydrated_cancelled = repeated_adapter.has_runtime_job(cancelled_job_id)
    repeated_cancelled_row = repeated_jobs.get(cancelled_job_id)
    if repeated_cancelled_row is not None and repeated_cancelled_row.enabled:
        repeated_adapter._dispatch(cancelled_job_id)
    repeated_restart_calls = dict(calls)
    state_after_repeated_restart = repeated_queue.get(cancelled_task_id).state
    repeated_adapter.shutdown(wait=False)

    actual = {
        "first_rehydrated_cancelled": first_rehydrated_cancelled,
        "first_restart_calls": first_restart_calls,
        "state_after_first_restart": state_after_first_restart,
        "cancelled_executable_after_first_restart": cancelled_executable_after_first_restart,
        "repeated_rehydrated_cancelled": repeated_rehydrated_cancelled,
        "repeated_restart_calls": repeated_restart_calls,
        "state_after_repeated_restart": state_after_repeated_restart,
    }
    expected = {
        "first_rehydrated_cancelled": False,
        "first_restart_calls": {"cancelled": 0, "unrelated": 1},
        "state_after_first_restart": TaskState.CANCELLED,
        "cancelled_executable_after_first_restart": False,
        "repeated_rehydrated_cancelled": False,
        "repeated_restart_calls": {"cancelled": 0, "unrelated": 1},
        "state_after_repeated_restart": TaskState.CANCELLED,
    }
    assert actual == expected
