from __future__ import annotations

from concurrent.futures import Future
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.agent_registry import AgentRegistry
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.workspace_registry import WorkspaceRegistry
from nika_core.ui.desktop_backend import DesktopBackend


def _backend(tmp_path: Path) -> DesktopBackend:
    store = SQLiteStore(tmp_path / "Дані Nika" / "ніка.db")
    store.initialize()
    return DesktopBackend(
        queue=TaskQueue(store),
        agents=AgentRegistry(store),
        workspaces=WorkspaceRegistry(store),
        audit=AuditLog(store),
    )


def test_stale_failed_runtime_callback_preserves_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _backend(tmp_path)
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(
        backend, "_record_background_failure", lambda task, event: events.append((task, event))
    )
    stale: Future[object] = Future()
    stale.set_exception(RuntimeError("old runtime failed"))
    current: Future[object] = Future()
    backend._active_threads["task"] = "current-thread"
    backend._active_futures["task"] = current

    # A finished future may be replaced before its callback acquires _active_lock.
    backend._runtime_done("task", stale)

    assert backend._active_futures["task"] is current
    assert backend._active_threads["task"] == "current-thread"
    assert events == [("task", "desktop.runtime_host_failed")]
    current.set_result(None)
    backend.close()


def test_stale_cancel_callback_preserves_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _backend(tmp_path)
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(
        backend, "_record_background_failure", lambda task, event: events.append((task, event))
    )
    stale: Future[bool] = Future()
    stale.set_result(False)
    current: Future[bool] = Future()
    backend._cancel_futures["task"] = current

    backend._cancel_done("task", stale)

    assert backend._cancel_futures["task"] is current
    assert events == [("task", "desktop.runtime_cancel_rejected")]
    current.set_result(True)
    backend.close()


def test_current_runtime_callback_clears_only_its_own_slot(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    current: Future[object] = Future()
    current.set_result(None)
    backend._active_threads["task"] = "current-thread"
    backend._active_futures["task"] = current

    backend._runtime_done("task", current)

    assert "task" not in backend._active_futures
    assert "task" not in backend._active_threads
    backend.close()


def test_current_cancel_callback_clears_only_its_own_slot(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    current: Future[bool] = Future()
    current.set_result(True)
    backend._cancel_futures["task"] = current

    backend._cancel_done("task", current)

    assert "task" not in backend._cancel_futures
    backend.close()


def test_replacement_future_remains_visible_to_shutdown(tmp_path: Path) -> None:
    backend = _backend(tmp_path)
    stale: Future[object] = Future()
    stale.set_result(None)
    current: Future[object] = Future()
    backend._active_threads["task"] = "current-thread"
    backend._active_futures["task"] = current
    backend._runtime_done("task", stale)

    with pytest.raises(RuntimeError, match="tasks are active"):
        backend.close()

    assert backend._active_futures["task"] is current
    current.set_result(None)
    backend.close()
    assert backend._active_futures == {}
