from __future__ import annotations

from datetime import UTC, datetime, timedelta

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.agent_registry import AgentRegistry
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.kernel.workspace_registry import WorkspaceRegistry
from nika_core.scheduler import APSchedulerAdapter, ScheduledJob, ScheduledJobStore, TriggerKind
from nika_core.ui.desktop_backend import DesktopBackend


def _task(queue: TaskQueue, *, terminal: TaskState | None = None) -> str:
    record = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "контрольована відкладена дія"},
    )
    queue.transition(record.task_id, TaskState.READY)
    queue.transition(record.task_id, TaskState.RUNNING)
    if terminal is TaskState.CANCELLED:
        queue.transition(record.task_id, TaskState.CANCELLED)
    elif terminal is TaskState.COMPLETED:
        queue.transition(record.task_id, TaskState.COMPLETED)
    elif terminal is TaskState.ARCHIVED:
        queue.transition(record.task_id, TaskState.COMPLETED)
        queue.transition(record.task_id, TaskState.ARCHIVED)
    return record.task_id


def _date_job(
    *,
    job_id: str,
    action_id: str,
    run_at: datetime,
    payload: dict[str, object],
) -> ScheduledJob:
    return ScheduledJob(
        job_id=job_id,
        action_id=action_id,
        trigger_kind=TriggerKind.DATE,
        trigger={"run_date": run_at.isoformat()},
        payload=payload,
        misfire_grace_seconds=3600,
    )


def test_terminal_task_authority_suppresses_rehydrated_wakes(tmp_path) -> None:
    db_path = tmp_path / "Ніка Runtime Authority" / "nika core.db"
    store = SQLiteStore(db_path)
    store.initialize()
    queue = TaskQueue(store)
    jobs = ScheduledJobStore(store)

    cancelled_id = _task(queue, terminal=TaskState.CANCELLED)
    completed_id = _task(queue, terminal=TaskState.COMPLETED)
    archived_id = _task(queue, terminal=TaskState.ARCHIVED)
    live_id = _task(queue)

    run_at = datetime.now(UTC) + timedelta(days=1)
    linked_jobs = {
        "cancelled": cancelled_id,
        "completed": completed_id,
        "archived": archived_id,
    }
    for action_id, task_id in linked_jobs.items():
        jobs.upsert(
            _date_job(
                job_id=f"job-{action_id}",
                action_id=action_id,
                run_at=run_at,
                payload={"task_id": task_id},
            )
        )
    jobs.upsert(
        _date_job(
            job_id="job-live",
            action_id="live",
            run_at=run_at,
            payload={"task_id": live_id},
        )
    )
    jobs.upsert(
        _date_job(
            job_id="job-unrelated",
            action_id="unrelated",
            run_at=run_at,
            payload={"kind": "maintenance"},
        )
    )
    jobs.upsert(
        _date_job(
            job_id="job-missing-task",
            action_id="missing-task",
            run_at=run_at,
            payload={"task_id": "missing-task-id"},
        )
    )
    jobs.upsert(
        _date_job(
            job_id="job-invalid-task",
            action_id="invalid-task",
            run_at=run_at,
            payload={"task_id": 123},
        )
    )

    calls: list[str] = []

    def resolve(action_id: str):
        def handler(_payload: dict[str, object]) -> None:
            calls.append(action_id)

        return handler

    adapter = APSchedulerAdapter(jobs, resolve, audit=AuditLog(store))
    adapter.start()
    for action_id in (*linked_jobs, "missing-task", "invalid-task", "live", "unrelated"):
        adapter._dispatch(f"job-{action_id}")

    assert calls == ["live", "unrelated"]
    for action_id in (*linked_jobs, "missing-task", "invalid-task"):
        assert jobs.get(f"job-{action_id}").enabled is False
    assert jobs.get("job-live").enabled is True
    assert jobs.get("job-unrelated").enabled is True
    adapter.shutdown(wait=False)

    reopened = SQLiteStore(db_path)
    reopened.initialize()
    reopened_jobs = ScheduledJobStore(reopened)
    reopened_audit = AuditLog(reopened)
    restarted = APSchedulerAdapter(reopened_jobs, resolve, audit=reopened_audit)
    restarted.start()
    for action_id in (*linked_jobs, "missing-task", "invalid-task"):
        assert not restarted.has_runtime_job(f"job-{action_id}")
        restarted._dispatch(f"job-{action_id}")
    assert calls == ["live", "unrelated"]
    restarted.shutdown(wait=False)

    restarted.resume("job-cancelled")
    assert reopened_jobs.get("job-cancelled").enabled is False
    cancelled_audit = reopened_audit.list_for(
        entity_type="scheduled_job",
        entity_id="job-cancelled",
    )
    assert cancelled_audit[-1].event_type == "scheduler.job_suppressed_task_authority"
    assert all(event.event_type != "scheduler.job_resumed" for event in cancelled_audit)

    third = APSchedulerAdapter(reopened_jobs, resolve, audit=reopened_audit)
    third.start()
    assert not third.has_runtime_job("job-cancelled")
    third._dispatch("job-cancelled")
    assert calls == ["live", "unrelated"]
    third.shutdown(wait=False)


def test_repeated_stop_of_one_cancelled_task_is_idempotent(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "Ніка Repeated Stop" / "nika core.db")
    store.initialize()
    queue = TaskQueue(store)
    task_id = _task(queue)
    queue.transition(task_id, TaskState.BLOCKED)
    backend = DesktopBackend(
        queue=queue,
        agents=AgentRegistry(store),
        workspaces=WorkspaceRegistry(store),
        audit=AuditLog(store),
    )

    first = backend.stop_agent({})
    assert first.status == "completed"
    assert queue.get(task_id).state is TaskState.CANCELLED
    with store.connection() as conn:
        events_before = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]

    repeated = backend.stop_agent({})

    with store.connection() as conn:
        events_after = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
    assert repeated.status == "completed"
    assert queue.get(task_id).state is TaskState.CANCELLED
    assert events_after == events_before
    backend.close()
