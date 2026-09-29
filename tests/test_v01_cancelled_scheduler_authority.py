from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

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


def test_upsert_rejects_foreign_job_before_attribute_behavior(tmp_path) -> None:
    class BehavioralJob:
        @property
        def job_id(self) -> str:
            raise AssertionError("foreign job attribute behavior must not run")

    store = SQLiteStore(tmp_path / "Ніка Scheduler Envelope Fence" / "nika core.db")
    store.initialize()
    jobs = ScheduledJobStore(store)
    adapter = APSchedulerAdapter(
        jobs,
        lambda _action_id: lambda _payload: None,
    )

    with pytest.raises(TypeError, match="job must be an exact ScheduledJob"):
        adapter.upsert(BehavioralJob())  # type: ignore[arg-type]

    assert jobs.list_enabled() == ()


def test_upsert_uses_durable_job_after_caller_payload_mutation(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "Ніка Scheduler Canonical Upsert" / "nika core.db")
    store.initialize()
    queue = TaskQueue(store)
    live_id = _task(queue)

    class PostPersistMutatingJobs(ScheduledJobStore):
        def upsert(self, job: ScheduledJob) -> None:
            super().upsert(job)
            job.payload["task_id"] = "missing-task-after-persist"

    jobs = PostPersistMutatingJobs(store)
    calls: list[str] = []

    def resolve(action_id: str):
        def handler(_payload: dict[str, object]) -> None:
            calls.append(action_id)

        return handler

    adapter = APSchedulerAdapter(jobs, resolve)
    adapter.start()
    caller_job = _date_job(
        job_id="job-live-race",
        action_id="live-race",
        run_at=datetime.now(UTC) + timedelta(days=1),
        payload={"task_id": live_id},
    )

    adapter.upsert(caller_job)

    durable = jobs.get("job-live-race")
    assert caller_job.payload["task_id"] == "missing-task-after-persist"
    assert durable is not None
    assert durable.payload == {"task_id": live_id}
    assert durable.enabled is True
    assert adapter.has_runtime_job("job-live-race")

    adapter._dispatch("job-live-race")

    assert calls == ["live-race"]
    adapter.shutdown(wait=False)


def test_dispatch_rechecks_durable_job_after_resolver_pause(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "Ніка Scheduler Dispatch Fence" / "nika core.db")
    store.initialize()
    queue = TaskQueue(store)
    live_id = _task(queue)
    jobs = ScheduledJobStore(store)
    calls: list[str] = []

    adapter: APSchedulerAdapter

    def resolve(action_id: str):
        assert action_id == "live-race"
        adapter.pause("job-live-race")

        def handler(_payload: dict[str, object]) -> None:
            calls.append(action_id)

        return handler

    adapter = APSchedulerAdapter(jobs, resolve)
    adapter.upsert(
        _date_job(
            job_id="job-live-race",
            action_id="live-race",
            run_at=datetime.now(UTC) + timedelta(days=1),
            payload={"task_id": live_id},
        )
    )
    adapter.start()

    adapter._dispatch("job-live-race")

    durable = jobs.get("job-live-race")
    assert durable is not None
    assert durable.enabled is False
    assert not adapter.has_runtime_job("job-live-race")
    assert calls == []
    adapter.shutdown(wait=False)


def test_resume_uses_durable_replacement_after_enable_interleaving(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "Ніка Scheduler Resume Fence" / "nika core.db")
    store.initialize()
    queue = TaskQueue(store)
    live_id = _task(queue)
    terminal_id = _task(queue, terminal=TaskState.CANCELLED)
    run_at = datetime.now(UTC) + timedelta(days=1)

    class ReplacingJobs(ScheduledJobStore):
        def set_enabled(self, job_id: str, enabled: bool) -> bool:
            if enabled:
                self.upsert(
                    _date_job(
                        job_id=job_id,
                        action_id="replacement",
                        run_at=run_at,
                        payload={"task_id": terminal_id},
                    )
                )
            return super().set_enabled(job_id, enabled)

    jobs = ReplacingJobs(store)
    adapter = APSchedulerAdapter(jobs, lambda _action_id: lambda _payload: None)
    adapter.upsert(
        _date_job(
            job_id="job-resume-race",
            action_id="original",
            run_at=run_at,
            payload={"task_id": live_id},
        )
    )
    adapter.start()
    adapter.pause("job-resume-race")
    assert not adapter.has_runtime_job("job-resume-race")

    adapter.resume("job-resume-race")

    durable = jobs.get("job-resume-race")
    assert durable is not None
    assert durable.action_id == "replacement"
    assert durable.payload == {"task_id": terminal_id}
    assert durable.enabled is False
    assert not adapter.has_runtime_job("job-resume-race")
    adapter.shutdown(wait=False)



def test_adapter_rejects_foreign_job_id_before_behavior(tmp_path) -> None:
    class BehavioralId(str):
        def strip(self, *args, **kwargs):
            del args, kwargs
            raise AssertionError("foreign job_id behavior must not run")

    store = SQLiteStore(tmp_path / "Ніка Scheduler ID Fence" / "nika core.db")
    store.initialize()
    jobs = ScheduledJobStore(store)
    adapter = APSchedulerAdapter(jobs, lambda _action_id: lambda _payload: None)
    hostile = _date_job(
        job_id=BehavioralId("job-hostile"),
        action_id="hostile",
        run_at=datetime.now(UTC) + timedelta(days=1),
        payload={},
    )

    with pytest.raises(TypeError, match="job_id must be an exact string"):
        adapter.upsert(hostile)

    assert jobs.get("job-hostile") is None


@pytest.mark.parametrize("operation", ("remove", "pause", "resume", "has_runtime_job"))
def test_adapter_job_id_operations_reject_foreign_text_before_behavior(
    tmp_path,
    operation: str,
) -> None:
    class BehavioralId(str):
        def strip(self, *args, **kwargs):
            del args, kwargs
            raise AssertionError("foreign job_id behavior must not run")

    store = SQLiteStore(tmp_path / f"Ніка Scheduler ID {operation}" / "nika core.db")
    store.initialize()
    jobs = ScheduledJobStore(store)
    jobs.upsert(
        _date_job(
            job_id="job-live",
            action_id="live",
            run_at=datetime.now(UTC) + timedelta(days=1),
            payload={},
        )
    )
    adapter = APSchedulerAdapter(jobs, lambda _action_id: lambda _payload: None)

    with pytest.raises(TypeError, match="job_id must be an exact string"):
        getattr(adapter, operation)(BehavioralId("job-live"))

    durable = jobs.get("job-live")
    assert durable is not None
    assert durable.enabled is True


def test_dispatch_removed_durable_job_is_noop(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "Ніка Scheduler Removed Dispatch" / "nika core.db")
    store.initialize()
    jobs = ScheduledJobStore(store)
    resolved: list[str] = []

    def resolve(action_id: str):
        resolved.append(action_id)
        return lambda _payload: None

    jobs.upsert(
        _date_job(
            job_id="job-removed",
            action_id="removed",
            run_at=datetime.now(UTC) + timedelta(days=1),
            payload={},
        )
    )
    adapter = APSchedulerAdapter(jobs, resolve)
    assert jobs.delete("job-removed") is True

    adapter._dispatch("job-removed")

    assert resolved == []


def test_resolver_failure_is_audited_before_handler_start(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "Ніка Scheduler Resolver Audit" / "nika core.db")
    store.initialize()
    jobs = ScheduledJobStore(store)
    audit = AuditLog(store)
    jobs.upsert(
        _date_job(
            job_id="job-resolver-failure",
            action_id="missing-handler",
            run_at=datetime.now(UTC) + timedelta(days=1),
            payload={},
        )
    )

    def resolve(_action_id: str):
        raise RuntimeError("resolver unavailable")

    adapter = APSchedulerAdapter(jobs, resolve, audit=audit)

    with pytest.raises(RuntimeError, match="resolver unavailable"):
        adapter._dispatch("job-resolver-failure")

    events = audit.list_for(
        entity_type="scheduled_job",
        entity_id="job-resolver-failure",
    )
    assert [event.event_type for event in events] == ["scheduler.job_failed"]
    assert events[0].payload == {
        "action_id": "missing-handler",
        "error_type": "RuntimeError",
    }


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
