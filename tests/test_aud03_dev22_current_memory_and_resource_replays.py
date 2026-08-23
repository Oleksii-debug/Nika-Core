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
from nika_core.resources import ResourceBudget, ResourceManager, ResourceSnapshot


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


def test_memory_put_returns_its_own_committed_value_after_later_overwrite(tmp_path: Path) -> None:
    path = tmp_path / "memory.db"
    store = SQLiteStore(path)
    store.initialize()
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
    assert current is not None and current.value == {"writer": "b"}


def test_restart_recovery_cannot_release_live_other_manager_lease(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "resource.db")
    store.initialize()
    holder = ResourceManager(store, _Observer(), manager_id="aud03-live-a")
    other = ResourceManager(store, _Observer(), manager_id="aud03-live-b")
    holder.set_budget(ResourceBudget(scope="project", owner_id="p1", max_concurrent=1))

    first = holder.request(scope="project", owner_id="p1", request_id="r1")
    assert first.granted
    with pytest.raises((ValueError, RuntimeError, PermissionError)):
        other.recover_after_restart(stale_manager_id=holder.manager_id)

    assert holder.active_count(scope="project", owner_id="p1") == 1
    second = other.request(scope="project", owner_id="p1", request_id="r2")
    assert not second.granted
    assert second.reason == "concurrency_limit"
