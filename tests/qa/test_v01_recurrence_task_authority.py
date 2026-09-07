from __future__ import annotations

import inspect
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.scheduler.apscheduler_adapter import APSchedulerAdapter
from nika_core.scheduler.recurrence import (
    DurableRecurrenceService,
    RecurrenceInvocation,
)
from nika_core.scheduler.store import ScheduledJobStore


@dataclass
class FakeClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "recurrence task authority.db")
    store.initialize()
    return store


def _runtime(
    store: SQLiteStore,
    clock: FakeClock,
    calls: list[RecurrenceInvocation],
) -> tuple[DurableRecurrenceService, APSchedulerAdapter, ScheduledJobStore]:
    jobs = ScheduledJobStore(store)
    service_ref: dict[str, DurableRecurrenceService] = {}

    def scheduler_resolver(action_id: str):
        assert action_id == DurableRecurrenceService.ACTION_ID
        return service_ref["service"].action_handler

    def target_resolver(action_id: str):
        assert action_id == "monitor.check"

        def handler(invocation: RecurrenceInvocation) -> None:
            calls.append(invocation)

        return handler

    adapter = APSchedulerAdapter(jobs, scheduler_resolver)
    service = DurableRecurrenceService(
        jobs=jobs,
        scheduler=adapter,
        handler_resolver=target_resolver,
        clock=clock,
    )
    service_ref["service"] = service
    return service, adapter, jobs


def _create_task_bound(
    service: DurableRecurrenceService,
    *,
    task_id: str,
    start_at: datetime,
) -> None:
    # Current #638 has no explicit task_id contract, which is the defect under test.
    # If the incumbent repair adds one, prefer that canonical boundary. Until then,
    # use the legacy target payload only to demonstrate that it is not projected to
    # ScheduledJob.payload where APSchedulerAdapter owns terminal-task authority.
    if "task_id" in inspect.signature(service.create).parameters:
        service.create(
            recurrence_id="task-bound-monitor",
            action_id="monitor.check",
            interval_seconds=60,
            start_at=start_at,
            task_id=task_id,  # type: ignore[call-arg]
            payload={"query": "rain"},
        )
        return
    service.create(
        recurrence_id="task-bound-monitor",
        action_id="monitor.check",
        interval_seconds=60,
        start_at=start_at,
        payload={"task_id": task_id, "query": "rain"},
    )


def test_task_bound_recurrence_projects_canonical_task_authority_to_scheduler(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    task = TaskQueue(store).create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"kind": "monitor"},
    )
    clock = FakeClock(datetime(2030, 1, 1, 12, 0, tzinfo=UTC))
    service, _adapter, jobs = _runtime(store, clock, [])

    _create_task_bound(service, task_id=task.task_id, start_at=clock.value)

    enabled = jobs.list_enabled()
    assert len(enabled) == 1
    assert enabled[0].payload.get("task_id") == task.task_id


def test_cancelled_task_suppresses_due_recurrence_after_restart(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    tasks = TaskQueue(store)
    task = tasks.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"kind": "monitor"},
    )
    clock = FakeClock(datetime(2030, 1, 1, 12, 0, tzinfo=UTC))
    initial_calls: list[RecurrenceInvocation] = []
    service, _adapter, jobs = _runtime(store, clock, initial_calls)

    _create_task_bound(service, task_id=task.task_id, start_at=clock.value)
    job_id = jobs.list_enabled()[0].job_id
    tasks.transition(task.task_id, TaskState.CANCELLED)

    restarted_calls: list[RecurrenceInvocation] = []
    _service, restarted_adapter, restarted_jobs = _runtime(
        SQLiteStore(store.path),
        clock,
        restarted_calls,
    )

    # Deterministically invoke the same scheduler-owned dispatch boundary that a
    # due APScheduler job uses. Terminal task authority must suppress the recurrence
    # before its target resolver/handler can run.
    restarted_adapter._dispatch(job_id)  # noqa: SLF001 - exact scheduler boundary oracle

    assert restarted_calls == []
    persisted = restarted_jobs.get(job_id)
    assert persisted is not None
    assert persisted.enabled is False
