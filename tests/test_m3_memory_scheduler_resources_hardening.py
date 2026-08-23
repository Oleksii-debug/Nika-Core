from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.memory import MemoryRetentionPolicy, MemoryScope, MemoryService
from nika_core.resources import (
    PsutilResourceObserver,
    ResourceBudget,
    ResourceManager,
    ResourceProcessIdentity,
    ResourceSnapshot,
)
from nika_core.scheduler import (
    APSchedulerAdapter,
    ScheduledJob,
    ScheduledJobStore,
    ScheduleIdentity,
    TriggerKind,
)


class FakeObserver:
    def __init__(
        self,
        *,
        cpu: float = 10.0,
        memory: float = 20.0,
        disk: float | None = 30.0,
        process_rss: int | None = 100_000,
        gpu: float | None = None,
    ) -> None:
        self.cpu = cpu
        self.memory = memory
        self.disk = disk
        self.process_rss = process_rss
        self.gpu = gpu

    def snapshot(self) -> ResourceSnapshot:
        return ResourceSnapshot(
            cpu_percent=self.cpu,
            memory_percent=self.memory,
            available_memory_bytes=2_000_000_000,
            disk_percent=self.disk,
            available_disk_bytes=500_000_000,
            process_rss_bytes=self.process_rss,
            gpu_percent=self.gpu,
        )


class FakeOwnerObserver(FakeObserver):
    def __init__(
        self,
        *,
        process_id: int,
        started_at: float,
        live_processes: set[tuple[int, float]],
    ) -> None:
        super().__init__()
        self._identity = ResourceProcessIdentity(
            process_id=process_id,
            started_at=started_at,
        )
        self._live_processes = live_processes

    def current_process_identity(self) -> ResourceProcessIdentity:
        return self._identity

    def is_process_alive(self, identity: ResourceProcessIdentity) -> bool:
        return (identity.process_id, identity.started_at) in self._live_processes


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    return store


def test_memory_scope_matrix_is_restart_safe_and_isolated(tmp_path: Path) -> None:
    store = _store(tmp_path)
    memory = MemoryService(store)
    scopes = (
        MemoryScope.SHORT_TERM,
        MemoryScope.TASK,
        MemoryScope.THREAD,
        MemoryScope.AGENT,
        MemoryScope.WORKSPACE,
        MemoryScope.USER_LONG_TERM,
    )
    for index, scope in enumerate(scopes):
        memory.put(
            scope=scope,
            owner_id="same-owner",
            namespace="same-namespace",
            key="same-key",
            value={"scope": scope.value, "index": index},
            user_approved=scope is MemoryScope.USER,
        )

    restarted = MemoryService(store)
    values = [
        restarted.get(
            scope=scope,
            owner_id="same-owner",
            namespace="same-namespace",
            key="same-key",
        )
        for scope in scopes
    ]
    assert all(record is not None for record in values)
    assert [record.value["index"] for record in values if record is not None] == list(
        range(len(scopes))
    )


def test_memory_retention_is_deterministic_across_restart(tmp_path: Path) -> None:
    store = _store(tmp_path)
    memory = MemoryService(store)
    start = datetime(2030, 1, 1, tzinfo=UTC)
    policy = MemoryRetentionPolicy(ttl_seconds=3600, max_records=2)

    for index, key in enumerate(("old", "middle", "new")):
        memory.put(
            scope=MemoryScope.THREAD,
            owner_id="thread-1",
            namespace="scratch",
            key=key,
            value=index,
            retention=policy,
            now=start + timedelta(seconds=index),
        )

    restarted = MemoryService(store)
    records = restarted.list_namespace(
        scope=MemoryScope.THREAD,
        owner_id="thread-1",
        namespace="scratch",
        now=start + timedelta(minutes=1),
    )
    assert [(record.key, record.value) for record in records] == [("middle", 1), ("new", 2)]
    assert restarted.list_namespace(
        scope=MemoryScope.THREAD,
        owner_id="thread-1",
        namespace="scratch",
        now=start + timedelta(hours=2),
    ) == ()


@pytest.mark.parametrize("field", ["ttl_seconds", "max_records"])
@pytest.mark.parametrize("value", [True, 0, -1, 1.5])
def test_memory_retention_rejects_non_positive_or_non_integer_values(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValueError):
        MemoryRetentionPolicy(**{field: value})


def test_memory_long_term_alias_still_requires_explicit_approval(tmp_path: Path) -> None:
    memory = MemoryService(_store(tmp_path))
    with pytest.raises(PermissionError):
        memory.put(
            scope=MemoryScope.USER_LONG_TERM,
            owner_id="user-1",
            namespace="preferences",
            key="language",
            value="uk",
        )


def _product_schedule(job_id: str = "nightly") -> ScheduledJob:
    return ScheduledJob(
        job_id=job_id,
        action_id="product.maintenance",
        trigger_kind=TriggerKind.INTERVAL,
        trigger={"hours": 24, "start_date": "2030-01-01T00:00:00+00:00"},
        payload={"operation": "maintain"},
        identity=ScheduleIdentity(
            scope="product_project",
            owner_id="project-1",
            dedup_key="maintenance",
            product_project_id="project-1",
        ),
    )


def test_schedule_identity_is_durable_and_deduplicated(tmp_path: Path) -> None:
    store = _store(tmp_path)
    jobs = ScheduledJobStore(store)
    jobs.upsert(_product_schedule())

    restarted = ScheduledJobStore(store)
    restored = restarted.get("nightly")
    assert restored == _product_schedule()
    assert restarted.list_for_owner(
        scope="product_project",
        owner_id="project-1",
    ) == (_product_schedule(),)

    with pytest.raises(ValueError, match="dedup key"):
        restarted.upsert(_product_schedule("duplicate"))


def test_schedule_identity_cannot_be_cleared_or_rebound(tmp_path: Path) -> None:
    jobs = ScheduledJobStore(_store(tmp_path))
    original = _product_schedule()
    jobs.upsert(original)

    with pytest.raises(ValueError, match="cannot be cleared"):
        jobs.upsert(
            ScheduledJob(
                job_id=original.job_id,
                action_id=original.action_id,
                trigger_kind=original.trigger_kind,
                trigger=original.trigger,
            )
        )

    with pytest.raises(ValueError, match="cannot be changed"):
        jobs.upsert(
            ScheduledJob(
                job_id=original.job_id,
                action_id=original.action_id,
                trigger_kind=original.trigger_kind,
                trigger=original.trigger,
                identity=ScheduleIdentity(
                    scope="product_project",
                    owner_id="project-1",
                    dedup_key="other",
                    product_project_id="project-1",
                ),
            )
        )


def test_scheduler_rejects_naive_time_and_preserves_identity_on_resume(tmp_path: Path) -> None:
    store = _store(tmp_path)
    jobs = ScheduledJobStore(store)
    with pytest.raises(ValueError, match="timezone-aware"):
        jobs.upsert(
            ScheduledJob(
                job_id="bad-time",
                action_id="noop",
                trigger_kind=TriggerKind.DATE,
                trigger={"run_date": "2030-01-01T00:00:00"},
            )
        )

    with pytest.raises(TypeError, match="ISO-8601"):
        jobs.upsert(
            ScheduledJob(
                job_id="bad-time-type",
                action_id="noop",
                trigger_kind=TriggerKind.DATE,
                trigger={"run_date": 17},
            )
        )

    adapter = APSchedulerAdapter(jobs, lambda _action_id: lambda _payload: None)
    adapter.upsert(_product_schedule())
    adapter.start()
    adapter.pause("nightly")
    adapter.shutdown()

    restarted = APSchedulerAdapter(
        ScheduledJobStore(store),
        lambda _action_id: lambda _payload: None,
    )
    restarted.start()
    restarted.resume("nightly")
    restored = ScheduledJobStore(store).get("nightly")
    assert restored is not None
    assert restored.identity == _product_schedule().identity
    restarted.shutdown()


def test_m3_extension_migration_fails_closed_on_future_version(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO m3_extension_schema_migrations(version, applied_at) VALUES (?, ?)",
            (3, datetime.now(UTC).isoformat()),
        )
    with pytest.raises(RuntimeError, match="newer than supported"):
        store.initialize()


def test_extended_resource_budget_persists_across_restart(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = ResourceManager(store, FakeObserver(), manager_id="manager-1")
    budget = ResourceBudget(
        scope="product_project",
        owner_id="project-1",
        max_concurrent=2,
        max_cpu_percent=80,
        max_memory_percent=85,
        max_disk_percent=90,
        max_gpu_percent=95,
        max_process_memory_bytes=512_000_000,
    )
    first.set_budget(budget)
    second = ResourceManager(store, FakeObserver(), manager_id="manager-2")
    assert second.get_budget(scope="product_project", owner_id="project-1") == budget


def test_resource_restart_releases_proven_dead_grant_and_preserves_fifo(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    live_processes = {(101, 1001.0), (202, 2002.0)}
    first_observer = FakeOwnerObserver(
        process_id=101,
        started_at=1001.0,
        live_processes=live_processes,
    )
    first = ResourceManager(store, first_observer, manager_id="manager-before-crash")
    first.set_budget(
        ResourceBudget(scope="product_project", owner_id="project-1", max_concurrent=1)
    )
    assert first.request(
        scope="product_project",
        owner_id="project-1",
        request_id="active",
        product_project_id="project-1",
    ).granted
    assert first.request(
        scope="product_project",
        owner_id="project-1",
        request_id="second",
        product_project_id="project-1",
    ).reason == "concurrency_limit"
    assert first.request(
        scope="product_project",
        owner_id="project-1",
        request_id="third",
        product_project_id="project-1",
    ).reason == "fifo_wait"

    live_processes.remove((101, 1001.0))
    restarted_observer = FakeOwnerObserver(
        process_id=202,
        started_at=2002.0,
        live_processes=live_processes,
    )
    restarted = ResourceManager(
        store,
        restarted_observer,
        manager_id="manager-after-restart",
    )
    blocked = restarted.request(
        scope="product_project",
        owner_id="project-1",
        request_id="active",
        product_project_id="project-1",
    )
    assert (blocked.granted, blocked.reason) == (False, "recovery_required")
    assert restarted.stale_lease_owners() == ("manager-before-crash",)
    assert restarted.recover_after_restart(stale_manager_id="manager-before-crash") == 1

    assert restarted.request(
        scope="product_project",
        owner_id="project-1",
        request_id="second",
        product_project_id="project-1",
    ).granted
    assert restarted.release(
        scope="product_project",
        owner_id="project-1",
        request_id="second",
    )
    assert restarted.request(
        scope="product_project",
        owner_id="project-1",
        request_id="third",
        product_project_id="project-1",
    ).granted
    assert restarted.release(
        scope="product_project",
        owner_id="project-1",
        request_id="third",
    )
    assert restarted.request(
        scope="product_project",
        owner_id="project-1",
        request_id="active",
        product_project_id="project-1",
    ).granted


def test_restart_recovery_cannot_release_a_live_resource_manager_lease(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    live_processes = {(301, 3001.0), (302, 3002.0)}
    holder = ResourceManager(
        store,
        FakeOwnerObserver(
            process_id=301,
            started_at=3001.0,
            live_processes=live_processes,
        ),
        manager_id="manager-live-a",
    )
    other = ResourceManager(
        store,
        FakeOwnerObserver(
            process_id=302,
            started_at=3002.0,
            live_processes=live_processes,
        ),
        manager_id="manager-live-b",
    )
    holder.set_budget(ResourceBudget(scope="project", owner_id="p1", max_concurrent=1))
    assert holder.request(scope="project", owner_id="p1", request_id="r1").granted

    with pytest.raises(RuntimeError, match="still alive"):
        other.recover_after_restart(stale_manager_id=holder.manager_id)

    assert holder.active_count(scope="project", owner_id="p1") == 1
    second = other.request(scope="project", owner_id="p1", request_id="r2")
    assert not second.granted
    assert second.reason == "concurrency_limit"


def test_restart_recovery_without_liveness_probe_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    holder = ResourceManager(store, FakeObserver(), manager_id="manager-live-a")
    other = ResourceManager(store, FakeObserver(), manager_id="manager-live-b")
    holder.set_budget(ResourceBudget(scope="project", owner_id="p1", max_concurrent=1))
    assert holder.request(scope="project", owner_id="p1", request_id="r1").granted

    with pytest.raises(RuntimeError, match="cannot be verified"):
        other.recover_after_restart(stale_manager_id=holder.manager_id)

    assert holder.active_count(scope="project", owner_id="p1") == 1
    second = other.request(scope="project", owner_id="p1", request_id="r2")
    assert second.reason == "concurrency_limit"


def test_reused_manager_id_cannot_adopt_or_release_an_old_lease(tmp_path: Path) -> None:
    store = _store(tmp_path)
    live_processes = {(401, 4001.0), (402, 4002.0)}
    holder = ResourceManager(
        store,
        FakeOwnerObserver(
            process_id=401,
            started_at=4001.0,
            live_processes=live_processes,
        ),
        manager_id="stable-manager",
    )
    holder.set_budget(ResourceBudget(scope="project", owner_id="p1", max_concurrent=2))
    assert holder.request(scope="project", owner_id="p1", request_id="r1").granted

    restarted = ResourceManager(
        store,
        FakeOwnerObserver(
            process_id=402,
            started_at=4002.0,
            live_processes=live_processes,
        ),
        manager_id="stable-manager",
    )
    replay = restarted.request(scope="project", owner_id="p1", request_id="r1")
    assert (replay.granted, replay.reason) == (False, "recovery_required")
    assert not restarted.release(scope="project", owner_id="p1", request_id="r1")
    new_request = restarted.request(scope="project", owner_id="p1", request_id="r2")
    assert (new_request.granted, new_request.reason) == (False, "recovery_required")

    live_processes.remove((401, 4001.0))
    assert restarted.recover_after_restart(stale_manager_id="stable-manager") == 1


def test_corrupt_resource_owner_process_identity_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    live_processes = {(501, 5001.0), (502, 5002.0)}
    holder = ResourceManager(
        store,
        FakeOwnerObserver(
            process_id=501,
            started_at=5001.0,
            live_processes=live_processes,
        ),
        manager_id="manager-a",
    )
    assert holder.request(scope="project", owner_id="p1", request_id="r1").granted
    with store.connection() as conn:
        conn.execute(
            """UPDATE resource_requests
            SET lease_owner_started_at = NULL
            WHERE scope = 'project' AND owner_id = 'p1' AND request_id = 'r1'"""
        )
    live_processes.remove((501, 5001.0))
    restarted = ResourceManager(
        store,
        FakeOwnerObserver(
            process_id=502,
            started_at=5002.0,
            live_processes=live_processes,
        ),
        manager_id="manager-b",
    )
    with pytest.raises(RuntimeError, match="no independently verifiable"):
        restarted.recover_after_restart(stale_manager_id="manager-a")


def test_resource_request_product_project_identity_cannot_drift(tmp_path: Path) -> None:
    manager = ResourceManager(_store(tmp_path), FakeObserver(), manager_id="manager")
    manager.request(
        scope="product_project",
        owner_id="project-1",
        request_id="work-1",
        product_project_id="project-1",
    )
    with pytest.raises(ValueError, match="ProductProject identity"):
        manager.request(
            scope="product_project",
            owner_id="project-1",
            request_id="work-1",
            product_project_id="project-2",
        )


def test_resource_observation_capability_policy_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    observer = FakeObserver(disk=None, process_rss=None, gpu=None)
    manager = ResourceManager(store, observer, manager_id="manager")
    manager.set_budget(
        ResourceBudget(
            scope="workspace",
            owner_id="w",
            max_disk_percent=80,
        )
    )
    assert manager.request(scope="workspace", owner_id="w", request_id="disk").reason == (
        "disk_unavailable"
    )

    manager.set_budget(
        ResourceBudget(
            scope="workspace",
            owner_id="gpu",
            max_gpu_percent=80,
        )
    )
    assert manager.request(scope="workspace", owner_id="gpu", request_id="gpu").reason == (
        "gpu_unavailable"
    )

    manager.set_budget(
        ResourceBudget(
            scope="workspace",
            owner_id="process",
            max_process_memory_bytes=1000,
        )
    )
    assert manager.request(
        scope="workspace",
        owner_id="process",
        request_id="process",
    ).reason == "process_memory_unavailable"


def test_resource_manager_serializes_concurrent_grants(tmp_path: Path) -> None:
    store = _store(tmp_path)
    manager = ResourceManager(store, FakeObserver(), manager_id="concurrent-manager")
    manager.set_budget(ResourceBudget(scope="workspace", owner_id="w", max_concurrent=1))

    def request(index: int) -> tuple[int, bool, str]:
        decision = manager.request(
            scope="workspace",
            owner_id="w",
            request_id=f"request-{index}",
        )
        return index, decision.granted, decision.reason

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(request, range(8)))

    assert sum(granted for _, granted, _ in results) == 1
    assert manager.active_count(scope="workspace", owner_id="w") == 1
    assert len(manager.queued(scope="workspace", owner_id="w")) == 7


def test_invalid_resource_observation_fails_closed(tmp_path: Path) -> None:
    manager = ResourceManager(
        _store(tmp_path),
        FakeObserver(cpu=float("nan")),
        manager_id="manager",
    )
    assert manager.request(scope="workspace", owner_id="w", request_id="r").reason == (
        "invalid_observation"
    )


def test_psutil_observer_reports_supported_local_metrics(tmp_path: Path) -> None:
    observer = PsutilResourceObserver(disk_path=tmp_path)
    snapshot = observer.snapshot()
    identity = observer.current_process_identity()
    assert 0 <= snapshot.cpu_percent <= 100
    assert 0 <= snapshot.memory_percent <= 100
    assert snapshot.available_memory_bytes >= 0
    assert snapshot.disk_percent is not None and 0 <= snapshot.disk_percent <= 100
    assert snapshot.available_disk_bytes is not None and snapshot.available_disk_bytes >= 0
    assert snapshot.process_rss_bytes is not None and snapshot.process_rss_bytes > 0
    assert snapshot.gpu_percent is None
    assert observer.is_process_alive(identity)
