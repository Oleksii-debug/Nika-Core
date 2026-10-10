from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.memory import MemoryRecord, MemoryScope, MemoryService
from nika_core.resources import (
    ResourceBudget,
    ResourceManager,
    ResourceProcessIdentity,
    ResourceSnapshot,
)


class _PauseAfterCommitStore(SQLiteStore):
    def __init__(self, path: Path, *, committed: Event, release: Event) -> None:
        super().__init__(path)
        self._committed = committed
        self._release = release
        self._pause_once = True

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        with super().connection() as conn:
            yield conn
        if self._pause_once:
            self._pause_once = False
            self._committed.set()
            assert self._release.wait(timeout=5), "writer A was not released"


class _Observer:
    def snapshot(self) -> ResourceSnapshot:
        return ResourceSnapshot(
            cpu_percent=1.0,
            memory_percent=1.0,
            available_memory_bytes=1_000_000,
            disk_percent=1.0,
            available_disk_bytes=1_000_000,
            process_rss_bytes=1_000,
            gpu_percent=None,
        )


class _Probe:
    def __init__(
        self,
        *,
        current: ResourceProcessIdentity,
        expected_owner: ResourceProcessIdentity | None = None,
        alive: bool = False,
    ) -> None:
        self._current = current
        self._expected_owner = expected_owner
        self._alive = alive
        self.probed: list[ResourceProcessIdentity] = []

    def current_process_identity(self) -> ResourceProcessIdentity:
        return self._current

    def is_process_alive(self, identity: ResourceProcessIdentity) -> bool:
        self.probed.append(identity)
        if self._expected_owner is not None:
            assert identity == self._expected_owner
        return self._alive


def test_memory_put_returns_its_own_commit_after_later_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    SQLiteStore(path).initialize()
    committed = Event()
    release = Event()
    writer_a = MemoryService(_PauseAfterCommitStore(path, committed=committed, release=release))
    writer_b = MemoryService(SQLiteStore(path))

    def write_a() -> MemoryRecord:
        return writer_a.put(
            scope=MemoryScope.WORKSPACE,
            owner_id="aud03-workspace",
            namespace="race",
            key="same-key",
            value={"writer": "a"},
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future_a = pool.submit(write_a)
        assert committed.wait(timeout=5), "writer A did not commit"
        returned_b = writer_b.put(
            scope=MemoryScope.WORKSPACE,
            owner_id="aud03-workspace",
            namespace="race",
            key="same-key",
            value={"writer": "b"},
        )
        release.set()
        returned_a = future_a.result(timeout=5)

    assert returned_a.value == {"writer": "a"}
    assert returned_b.value == {"writer": "b"}
    current = writer_b.get(
        scope=MemoryScope.WORKSPACE,
        owner_id="aud03-workspace",
        namespace="race",
        key="same-key",
    )
    assert current is not None
    assert current.value == {"writer": "b"}


@pytest.mark.parametrize("corrupt_flag", [0, 2])
def test_user_memory_corrupt_durable_approval_fails_closed(
    tmp_path: Path,
    corrupt_flag: int,
) -> None:
    path = tmp_path / f"memory-approval-{corrupt_flag}.db"
    store = SQLiteStore(path)
    store.initialize()
    memory = MemoryService(store)
    memory.put(
        scope=MemoryScope.USER_LONG_TERM,
        owner_id="aud03-user",
        namespace="preferences",
        key="language",
        value="uk",
        user_approved=True,
    )

    with sqlite3.connect(path) as conn:
        if corrupt_flag == 2:
            conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute(
            "UPDATE memory_records SET user_approved=? "
            "WHERE scope=? AND owner_id=? AND namespace=? AND memory_key=?",
            (
                corrupt_flag,
                MemoryScope.USER.value,
                "aud03-user",
                "preferences",
                "language",
            ),
        )

    with pytest.raises(RuntimeError, match="approval"):
        memory.get(
            scope=MemoryScope.USER_LONG_TERM,
            owner_id="aud03-user",
            namespace="preferences",
            key="language",
        )


def test_restart_recovery_without_probe_cannot_release_other_manager_lease(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "resource-no-probe.db")
    store.initialize()
    holder = ResourceManager(store, _Observer(), manager_id="aud03-live-a")
    recoverer = ResourceManager(store, _Observer(), manager_id="aud03-live-b")
    holder.set_budget(ResourceBudget(scope="project", owner_id="p1", max_concurrent=1))

    first = holder.request(scope="project", owner_id="p1", request_id="r1")
    assert first.granted
    with pytest.raises(RuntimeError, match="liveness cannot be verified"):
        recoverer.recover_after_restart(stale_manager_id=holder.manager_id)

    assert holder.active_count(scope="project", owner_id="p1") == 1
    second = recoverer.request(scope="project", owner_id="p1", request_id="r2")
    assert not second.granted
    assert second.reason == "concurrency_limit"


def test_restart_recovery_probe_alive_preserves_exact_process_generation_lease(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "resource-live-probe.db")
    store.initialize()
    owner_identity = ResourceProcessIdentity(process_id=1111, started_at=1000.0)
    recoverer_identity = ResourceProcessIdentity(process_id=2222, started_at=2000.0)
    holder_probe = _Probe(current=owner_identity)
    recoverer_probe = _Probe(
        current=recoverer_identity,
        expected_owner=owner_identity,
        alive=True,
    )
    holder = ResourceManager(
        store,
        _Observer(),
        manager_id="aud03-owner-a",
        owner_probe=holder_probe,
    )
    recoverer = ResourceManager(
        store,
        _Observer(),
        manager_id="aud03-owner-b",
        owner_probe=recoverer_probe,
    )
    holder.set_budget(ResourceBudget(scope="project", owner_id="p-live", max_concurrent=1))

    first = holder.request(scope="project", owner_id="p-live", request_id="r-live")
    assert first.granted
    with pytest.raises(RuntimeError, match="owner is still alive"):
        recoverer.recover_after_restart(stale_manager_id=holder.manager_id)

    assert recoverer_probe.probed == [owner_identity]
    assert holder.active_count(scope="project", owner_id="p-live") == 1
    second = recoverer.request(scope="project", owner_id="p-live", request_id="r-second")
    assert not second.granted
    assert second.reason == "concurrency_limit"
