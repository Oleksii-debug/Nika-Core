from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.memory import MemoryScope, MemoryService
from nika_core.resources import ResourceBudget, ResourceManager, ResourceSnapshot
from nika_core.scheduler import APSchedulerAdapter, ScheduledJob, ScheduledJobStore, TriggerKind


class FakeObserver:
    def __init__(self, *, cpu: float = 10.0, memory: float = 20.0) -> None:
        self.cpu = cpu
        self.memory = memory

    def snapshot(self) -> ResourceSnapshot:
        return ResourceSnapshot(
            cpu_percent=self.cpu,
            memory_percent=self.memory,
            available_memory_bytes=2_000_000_000,
        )


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    return store


def test_memory_scopes_are_durable_isolated_and_audited(tmp_path: Path) -> None:
    store = _store(tmp_path)
    audit = AuditLog(store)
    memory = MemoryService(store, audit)
    memory.put(
        scope=MemoryScope.WORKSPACE,
        owner_id="research",
        namespace="sources",
        key="policy",
        value={"verified": True},
    )
    memory.put(
        scope=MemoryScope.AGENT,
        owner_id="research",
        namespace="sources",
        key="policy",
        value={"verified": False},
    )

    restarted = MemoryService(store, audit)
    workspace = restarted.get(
        scope=MemoryScope.WORKSPACE,
        owner_id="research",
        namespace="sources",
        key="policy",
    )
    agent = restarted.get(
        scope=MemoryScope.AGENT,
        owner_id="research",
        namespace="sources",
        key="policy",
    )
    assert workspace is not None and workspace.value == {"verified": True}
    assert agent is not None and agent.value == {"verified": False}
    assert len(audit.list_for(entity_type="memory", entity_id="workspace:research:sources:policy")) == 1


def test_user_long_term_memory_requires_explicit_approval(tmp_path: Path) -> None:
    memory = MemoryService(_store(tmp_path))
    with pytest.raises(PermissionError):
        memory.put(
            scope=MemoryScope.USER,
            owner_id="local-user",
            namespace="preferences",
            key="language",
            value="uk",
        )
    record = memory.put(
        scope=MemoryScope.USER,
        owner_id="local-user",
        namespace="preferences",
        key="language",
        value="uk",
        user_approved=True,
    )
    assert record.user_approved is True


def test_expired_memory_is_not_returned_and_can_be_purged(tmp_path: Path) -> None:
    memory = MemoryService(_store(tmp_path))
    expires = datetime.now(UTC) + timedelta(minutes=5)
    memory.put(
        scope=MemoryScope.TASK,
        owner_id="task-1",
        namespace="scratch",
        key="temporary",
        value=123,
        expires_at=expires,
    )
    assert memory.get(
        scope=MemoryScope.TASK,
        owner_id="task-1",
        namespace="scratch",
        key="temporary",
        now=expires + timedelta(seconds=1),
    ) is None


def test_memory_rejects_naive_expiry_datetime(tmp_path: Path) -> None:
    memory = MemoryService(_store(tmp_path))
    naive_expiry = datetime(2030, 1, 1, tzinfo=UTC).replace(tzinfo=None)
    with pytest.raises(ValueError):
        memory.put(
            scope=MemoryScope.TASK,
            owner_id="task-1",
            namespace="scratch",
            key="bad",
            value=True,
            expires_at=naive_expiry,
        )


def test_scheduler_rehydrates_persisted_jobs_after_restart(tmp_path: Path) -> None:
    store = _store(tmp_path)
    jobs = ScheduledJobStore(store)
    calls: list[dict[str, object]] = []

    def resolver(action_id: str):
        assert action_id == "maintenance.cleanup"
        return calls.append

    run_at = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    first = APSchedulerAdapter(jobs, resolver)
    first.upsert(
        ScheduledJob(
            job_id="cleanup",
            action_id="maintenance.cleanup",
            trigger_kind=TriggerKind.DATE,
            trigger={"run_date": run_at},
            payload={"scope": "expired-memory"},
        )
    )
    first.start()
    assert first.has_runtime_job("cleanup")
    first.shutdown()

    restarted = APSchedulerAdapter(ScheduledJobStore(store), resolver)
    restarted.start()
    assert restarted.has_runtime_job("cleanup")
    restarted._dispatch("cleanup")
    assert calls == [{"scope": "expired-memory"}]
    restarted.shutdown()


def test_scheduler_pause_survives_restart_and_resume_reinstalls(tmp_path: Path) -> None:
    store = _store(tmp_path)
    jobs = ScheduledJobStore(store)
    resolver = lambda _action_id: (lambda _payload: None)
    adapter = APSchedulerAdapter(jobs, resolver)
    adapter.upsert(
        ScheduledJob(
            job_id="heartbeat",
            action_id="health.sample",
            trigger_kind=TriggerKind.INTERVAL,
            trigger={"minutes": 10},
        )
    )
    adapter.start()
    adapter.pause("heartbeat")
    assert not adapter.has_runtime_job("heartbeat")
    adapter.shutdown()

    restarted = APSchedulerAdapter(ScheduledJobStore(store), resolver)
    restarted.start()
    assert not restarted.has_runtime_job("heartbeat")
    restarted.resume("heartbeat")
    assert restarted.has_runtime_job("heartbeat")
    restarted.shutdown()


def test_scheduler_rejects_invalid_job_contract(tmp_path: Path) -> None:
    jobs = ScheduledJobStore(_store(tmp_path))
    with pytest.raises(ValueError):
        jobs.upsert(
            ScheduledJob(
                job_id="bad",
                action_id="health.sample",
                trigger_kind=TriggerKind.INTERVAL,
                trigger={"seconds": 1},
                max_instances=0,
            )
        )


def test_resource_budget_persists_across_manager_restart(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = ResourceManager(store, FakeObserver())
    first.set_budget(
        ResourceBudget(
            scope="workspace",
            owner_id="research",
            max_concurrent=2,
            max_cpu_percent=80,
            max_memory_percent=85,
        )
    )
    second = ResourceManager(store, FakeObserver())
    assert second.get_budget(scope="workspace", owner_id="research") == ResourceBudget(
        scope="workspace",
        owner_id="research",
        max_concurrent=2,
        max_cpu_percent=80.0,
        max_memory_percent=85.0,
    )


def test_resource_manager_enforces_fifo_concurrency(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manager = ResourceManager(store, FakeObserver())
    manager.set_budget(ResourceBudget(scope="workspace", owner_id="research", max_concurrent=1))

    assert manager.request(scope="workspace", owner_id="research", request_id="first").granted
    second = manager.request(scope="workspace", owner_id="research", request_id="second")
    third = manager.request(scope="workspace", owner_id="research", request_id="third")
    assert (second.granted, second.reason, second.queue_position) == (False, "concurrency_limit", 1)
    assert (third.granted, third.reason, third.queue_position) == (False, "fifo_wait", 2)

    assert manager.release(scope="workspace", owner_id="research", request_id="first")
    assert manager.request(scope="workspace", owner_id="research", request_id="third").reason == "fifo_wait"
    assert manager.request(scope="workspace", owner_id="research", request_id="second").granted


def test_resource_manager_blocks_cpu_and_memory_pressure(tmp_path: Path) -> None:
    store = _store(tmp_path)
    observer = FakeObserver(cpu=95, memory=20)
    manager = ResourceManager(store, observer)
    manager.set_budget(
        ResourceBudget(
            scope="agent",
            owner_id="transcriber",
            max_concurrent=1,
            max_cpu_percent=80,
            max_memory_percent=80,
        )
    )
    assert manager.request(scope="agent", owner_id="transcriber", request_id="job-1").reason == "cpu_limit"
    observer.cpu = 20
    observer.memory = 90
    assert manager.request(scope="agent", owner_id="transcriber", request_id="job-1").reason == "memory_limit"
    observer.memory = 20
    assert manager.request(scope="agent", owner_id="transcriber", request_id="job-1").granted


def test_waiting_resource_request_can_be_cancelled(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manager = ResourceManager(store, FakeObserver())
    manager.set_budget(ResourceBudget(scope="workspace", owner_id="w", max_concurrent=1))
    assert manager.request(scope="workspace", owner_id="w", request_id="active").granted
    assert not manager.request(scope="workspace", owner_id="w", request_id="waiting").granted
    assert manager.cancel_waiting(scope="workspace", owner_id="w", request_id="waiting")
    assert manager.queued(scope="workspace", owner_id="w") == ()
