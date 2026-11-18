from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.resources import PsutilResourceObserver, ResourceManager
from scripts import nika_windows

_SCOPE = "desktop_runtime"
_OWNER = "local_machine"
_CRASH = """
import os, sys
from pathlib import Path
from nika_core.data.sqlite import SQLiteStore
from nika_core.resources import PsutilResourceObserver, ResourceManager
store = SQLiteStore(Path(sys.argv[1]))
store.initialize()
manager = ResourceManager(store, PsutilResourceObserver(), manager_id="lost-owner")
if not manager.request(
    scope="desktop_runtime", owner_id="local_machine", request_id="lost-request"
).granted:
    raise SystemExit(2)
os._exit(0)
"""


def _config(tmp_path: Path) -> AppConfig:
    root = tmp_path / "Дані ресурсів"
    root.mkdir()
    source_a, source_b = root / "а.txt", root / "б.txt"
    source_a.write_text("Перше джерело.", encoding="utf-8")
    source_b.write_text("Друге джерело.", encoding="utf-8")
    return AppConfig(
        database_path=root / "ніка.db", v01_source_root=root,
        v01_source_a=source_a, v01_source_b=source_b,
    )


def _crash_with_grant(config: AppConfig) -> None:
    root = Path(__file__).resolve().parents[1]
    subprocess.run(
        [sys.executable, "-c", _CRASH, str(config.database_path)],
        cwd=root, env={**os.environ, "PYTHONPATH": str(root / "src")},
        check=True, timeout=30, capture_output=True, text=True,
    )


def _build(config: AppConfig, monkeypatch: pytest.MonkeyPatch):
    captured = []
    actual = nika_windows.DesktopBackend

    def capture(**kwargs):
        backend = actual(**kwargs)
        captured.append(backend)
        return backend

    monkeypatch.setattr(nika_windows, "DesktopBackend", capture)
    nika_windows.build_windows_bridge(config)
    assert len(captured) == 1
    return captured[0]


def _wait_for_state(queue: TaskQueue, task_id: str, expected: TaskState) -> None:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if queue.get(task_id).state is expected:
            return
        time.sleep(0.01)
    assert queue.get(task_id).state is expected


def test_packaged_startup_recovers_real_dead_process_before_new_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    _crash_with_grant(config)
    store = SQLiteStore(config.database_path)
    queue = TaskQueue(store)
    assert not queue.list_recent()
    backend = _build(config, monkeypatch)
    manager = backend._resources
    assert manager is not None
    try:
        assert manager.active_count(scope=_SCOPE, owner_id=_OWNER) == 0
        assert not queue.list_recent()  # Resource recovery never replays tasks.
        backend.create_task({"command": "Перевірити джерела після аварії"})
        task_id = queue.list_recent()[0].task_id
        _wait_for_state(queue, task_id, TaskState.COMPLETED)
        events = AuditLog(store).list_for(entity_type="resource_pool", entity_id=_OWNER)
        assert any(event.payload.get("released_leases") == 1 for event in events)
    finally:
        backend.close()


@pytest.mark.parametrize("proof", ("unknown", "corrupt"))
def test_packaged_startup_preserves_unproven_lease_without_runtime_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, proof: str,
) -> None:
    config = _config(tmp_path)
    _crash_with_grant(config)
    store = SQLiteStore(config.database_path)
    if proof == "unknown":
        monkeypatch.setattr(PsutilResourceObserver, "is_process_alive", lambda *_: None)
    else:
        with store.connection() as conn:
            conn.execute("UPDATE resource_requests SET lease_owner_process_id = NULL")
    backend = _build(config, monkeypatch)
    manager = backend._resources
    assert manager is not None
    queue = TaskQueue(store)
    try:
        assert manager.active_count(scope=_SCOPE, owner_id=_OWNER) == 1
        assert not queue.list_recent()
        backend.create_task({"command": "Очікувати перевірки ресурсу"})
        task_id = queue.list_recent()[0].task_id
        _wait_for_state(queue, task_id, TaskState.PAUSED)
        assert backend._coordinator.sessions.get(task_id) is None
        assert manager.active_count(scope=_SCOPE, owner_id=_OWNER) == 1
        events = AuditLog(store).list_for(entity_type="resource_pool", entity_id=_OWNER)
        assert any(event.payload.get("outcome") == "blocked" for event in events)
        assert all("lost-owner" not in str(event.payload) for event in events)
    finally:
        backend.close()


def test_packaged_startup_preserves_live_process_lease(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    store = SQLiteStore(config.database_path)
    store.initialize()
    owner = ResourceManager(store, PsutilResourceObserver(), manager_id="live-owner")
    assert owner.request(scope=_SCOPE, owner_id=_OWNER, request_id="live-request").granted
    backend = _build(config, monkeypatch)
    manager = backend._resources
    assert manager is not None
    try:
        assert manager.active_count(scope=_SCOPE, owner_id=_OWNER) == 1
        assert manager.request(
            scope=_SCOPE, owner_id=_OWNER, request_id="new-request"
        ).reason == "concurrency_limit"
        assert owner.release(scope=_SCOPE, owner_id=_OWNER, request_id="live-request")
    finally:
        backend.close()
