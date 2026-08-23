from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.resources import ResourceBudget, ResourceManager, ResourceSnapshot


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


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    return store


def test_restart_recovery_cannot_release_a_live_resource_manager_lease(tmp_path: Path) -> None:
    store = _store(tmp_path)
    holder = ResourceManager(store, _Observer(), manager_id="manager-live-a")
    other = ResourceManager(store, _Observer(), manager_id="manager-live-b")
    holder.set_budget(ResourceBudget(scope="project", owner_id="p1", max_concurrent=1))

    first = holder.request(scope="project", owner_id="p1", request_id="r1")
    assert first.granted
    assert holder.active_count(scope="project", owner_id="p1") == 1

    with pytest.raises((ValueError, RuntimeError, PermissionError)):
        other.recover_after_restart(stale_manager_id=holder.manager_id)

    assert holder.active_count(scope="project", owner_id="p1") == 1
    second = other.request(scope="project", owner_id="p1", request_id="r2")
    assert not second.granted
    assert second.reason == "concurrency_limit"
