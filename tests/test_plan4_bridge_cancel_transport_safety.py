from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.action_registry import ActionRegistry, Keymap
from nika_core.kernel.agent_registry import AgentRegistry
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.default_actions import build_default_action_registry
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.kernel.workspace_registry import WorkspaceRegistry
from nika_core.runtime.contracts import RuntimeCapability
from nika_core.ui.bridge import UIActionBridge
from nika_core.ui.desktop_backend import DesktopBackend


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    return store


def test_action_lookup_unexpected_failure_is_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    store = _store(tmp_path)
    actions = build_default_action_registry()
    called = []

    def leak(_self: ActionRegistry, _action_id: str) -> None:
        raise RuntimeError("private-token-must-not-leak")

    monkeypatch.setattr(ActionRegistry, "get", leak)
    bridge = UIActionBridge(
        actions, Keymap(store, actions),
        handlers={"task.create": lambda _payload: called.append(True)},
    )
    result = bridge.dispatch(
        {"request_id": "registry-failure", "action_id": "task.create", "payload": {}}
    )
    assert result == {
        "request_id": "registry-failure",
        "status": "failed",
        "message": "Не вдалося виконати дію через внутрішню помилку.",
        "focus_id": None,
    }
    assert called == []
    assert "private-token-must-not-leak" not in str(result)
    assert "private-token-must-not-leak" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_action_lookup_shutdown_signal_is_not_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    actions = build_default_action_registry()
    bridge = UIActionBridge(actions, Keymap(store, actions))

    def interrupt(_self: ActionRegistry, _action_id: str) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(ActionRegistry, "get", interrupt)
    with pytest.raises(KeyboardInterrupt):
        bridge.dispatch(
            {"request_id": "shutdown", "action_id": "task.create", "payload": {}}
        )


def test_stop_host_submission_failure_closes_cancellation_without_state_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path)
    queue = TaskQueue(store)
    backend = DesktopBackend(
        queue=queue,
        agents=AgentRegistry(store),
        workspaces=WorkspaceRegistry(store),
        audit=AuditLog(store),
    )
    record = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "cancel safely"},
    )
    queue.transition(record.task_id, TaskState.READY)
    backend._runtime = SimpleNamespace(
        runtime_id="cancellable-test-runtime",
        capabilities=frozenset({RuntimeCapability.CANCELLATION}),
    )
    backend._active_threads[record.task_id] = "desktop-owned-thread"
    captured = []

    class FailedHost:
        def submit(self, coroutine):
            captured.append(coroutine)
            raise RuntimeError("host submission unavailable")

    monkeypatch.setattr(backend, "_host", lambda: FailedHost())

    try:
        with pytest.raises(RuntimeError, match="host submission unavailable"):
            backend.stop_agent({"task_id": record.task_id})
        assert len(captured) == 1
        assert inspect.getcoroutinestate(captured[0]) == inspect.CORO_CLOSED
        assert backend._cancel_futures == {}
        assert queue.get(record.task_id).state == TaskState.READY
    finally:
        backend.close()
