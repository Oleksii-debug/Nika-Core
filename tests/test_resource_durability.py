from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.resources import (
    PsutilResourceObserver,
    ResourceBudget,
    ResourceManager,
    ResourceProcessIdentity,
    ResourceSnapshot,
    psutil_adapter,
)


class FakeObserver:
    def __init__(self, *, cpu: float = 10.0, memory: float = 20.0) -> None:
        self.cpu = cpu
        self.memory = memory

    def snapshot(self) -> ResourceSnapshot:
        return ResourceSnapshot(self.cpu, self.memory, 2_000_000_000)


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


def test_durable_resource_queue_and_read_only_capacity_status_compose(tmp_path: Path) -> None:
    store = _store(tmp_path)
    observer = FakeObserver(cpu=25.0, memory=30.0)
    manager = ResourceManager(store, observer, manager_id="status-manager")
    manager.set_budget(
        ResourceBudget(
            scope="workspace",
            owner_id="w",
            max_concurrent=1,
            max_cpu_percent=80.0,
            max_memory_percent=85.0,
        )
    )

    assert manager.request(scope="workspace", owner_id="w", request_id="active").granted
    waiting = manager.request(scope="workspace", owner_id="w", request_id="waiting")
    assert (waiting.granted, waiting.reason) == (False, "concurrency_limit")
    before = manager.queued(scope="workspace", owner_id="w")

    status = manager.status(scope="workspace", owner_id="w")

    assert status.active_count == 1
    assert status.queued_count == 1
    assert status.concurrency_headroom == 0
    assert status.cpu_headroom_percent == pytest.approx(55.0)
    assert status.memory_headroom_percent == pytest.approx(55.0)
    assert status.pressure_reasons == ("concurrency_limit",)
    assert manager.queued(scope="workspace", owner_id="w") == before

    restarted = ResourceManager(store, observer, manager_id="status-manager-after-restart")
    restarted_status = restarted.status(scope="workspace", owner_id="w")
    assert restarted_status.active_count == 1
    assert restarted_status.queued_count == 1
    assert restarted.queued(scope="workspace", owner_id="w") == ("waiting",)


def test_independent_shared_store_managers_serialize_one_slot(tmp_path: Path) -> None:
    store = _store(tmp_path)
    managers = [
        ResourceManager(SQLiteStore(store.path), FakeObserver(), manager_id=f"manager-{index}")
        for index in range(8)
    ]

    def request(index: int) -> bool:
        return managers[index].request(
            scope="machine", owner_id="host", request_id=f"request-{index}"
        ).granted

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(request, range(8)))

    assert sum(results) == 1
    for manager in managers:
        assert manager.active_count(scope="machine", owner_id="host") == 1
        assert len(manager.queued(scope="machine", owner_id="host")) == 7


@pytest.mark.parametrize("proof", [None, 0, "", object()])
def test_recovery_requires_exact_dead_bool(tmp_path: Path, proof: object) -> None:
    store = _store(tmp_path)
    live = {(801, 8001.0), (802, 8002.0)}
    holder = ResourceManager(
        store,
        FakeOwnerObserver(process_id=801, started_at=8001.0, live_processes=live),
        manager_id="holder",
    )
    assert holder.request(scope="s", owner_id="o", request_id="r").granted

    class UncertainProbe(FakeOwnerObserver):
        def is_process_alive(self, identity: ResourceProcessIdentity) -> bool:
            return proof  # type: ignore[return-value]

    other = ResourceManager(
        store,
        UncertainProbe(process_id=802, started_at=8002.0, live_processes=live),
    )
    with pytest.raises(RuntimeError, match="cannot be verified"):
        other.recover_after_restart(stale_manager_id="holder")
    assert other.active_count(scope="s", owner_id="o") == 1


def test_recovery_rejects_mixed_manager_generations(tmp_path: Path) -> None:
    store = _store(tmp_path)
    live = {(901, 9001.0)}
    holder = ResourceManager(
        store,
        FakeOwnerObserver(process_id=901, started_at=9001.0, live_processes=live),
        manager_id="mixed",
    )
    holder.set_budget(ResourceBudget(scope="s", owner_id="o", max_concurrent=2))
    for request_id in ("one", "two"):
        assert holder.request(scope="s", owner_id="o", request_id=request_id).granted
    with store.connection() as conn:
        conn.execute(
            "UPDATE resource_requests SET lease_owner_started_at = 9002 WHERE request_id = 'two'"
        )
    live.clear()
    with pytest.raises(RuntimeError, match="multiple process identities"):
        holder.recover_after_restart(stale_manager_id="mixed")
    assert holder.active_count(scope="s", owner_id="o") == 2


def test_old_instance_cannot_release_reused_manager_generation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    live = {(1001, 10001.0)}
    old = ResourceManager(
        store,
        FakeOwnerObserver(process_id=1001, started_at=10001.0, live_processes=live),
        manager_id="reused",
    )
    assert old.request(scope="s", owner_id="o", request_id="r").granted
    live.clear()
    new = ResourceManager(
        store,
        FakeOwnerObserver(process_id=1002, started_at=10002.0, live_processes=live),
        manager_id="reused",
    )
    assert new.recover_after_restart(stale_manager_id="reused") == 1
    assert new.request(scope="s", owner_id="o", request_id="r").granted
    assert not old.release(scope="s", owner_id="o", request_id="r")
    assert not old.revalidate(scope="s", owner_id="o", request_id="r").granted
    assert new.active_count(scope="s", owner_id="o") == 1


def test_psutil_owner_generation_and_pid_reuse_fail_closed(monkeypatch) -> None:
    identity = ResourceProcessIdentity(51, 5001.0)
    observer = PsutilResourceObserver()
    assert observer.is_process_alive(observer.current_process_identity())

    class ReusedPid:
        def create_time(self) -> float:
            return 5002.0

    monkeypatch.setattr(psutil_adapter.psutil, "Process", lambda _pid: ReusedPid())
    assert observer.is_process_alive(identity) is False

    def inaccessible(_pid):
        raise psutil_adapter.psutil.AccessDenied(51)

    monkeypatch.setattr(psutil_adapter.psutil, "Process", inaccessible)
    assert observer.is_process_alive(identity) is True


@pytest.mark.parametrize("cpu, expected", [(95.0, "cpu_limit"), (15.0, "granted")])
def test_first_psutil_admission_uses_a_measured_cpu_sample(
    tmp_path, monkeypatch, cpu, expected
) -> None:
    from types import SimpleNamespace

    intervals = []

    def sample(*, interval):
        intervals.append(interval)
        return cpu if interval == 0.1 else 0.0

    monkeypatch.setattr(psutil_adapter.psutil, "cpu_percent", sample)
    monkeypatch.setattr(
        psutil_adapter.psutil, "virtual_memory",
        lambda: SimpleNamespace(percent=20.0, available=1000, total=2000),
    )
    observer = PsutilResourceObserver()
    manager = ResourceManager(_store(tmp_path), observer)
    manager.set_budget(ResourceBudget(scope="s", owner_id="o", max_cpu_percent=80.0))
    assert manager.request(scope="s", owner_id="o", request_id="r").reason == expected
    assert intervals == [0.1]


@pytest.mark.parametrize("started_at", [float("nan"), float("inf"), True, -1, None])
def test_psutil_invalid_process_generation_never_means_dead(monkeypatch, started_at) -> None:
    class CorruptProcess:
        def create_time(self):
            return started_at

    observer = PsutilResourceObserver()
    monkeypatch.setattr(psutil_adapter.psutil, "Process", lambda _pid: CorruptProcess())
    with pytest.raises((ValueError, TypeError)):
        observer.is_process_alive(ResourceProcessIdentity(51, 5001.0))
