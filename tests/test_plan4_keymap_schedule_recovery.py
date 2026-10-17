"""Plan 4 §§1–2 regression: stored NVDA shortcuts and failed runtime scheduling."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.action_registry import ActionDefinition, ActionRegistry, Keymap
from nika_core.kernel.agent_registry import AgentRegistry
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.kernel.workspace_registry import WorkspaceRegistry
from nika_core.ui.bridge import UIActionBridge
from nika_core.ui.desktop_backend import DesktopBackend


def _keymap(tmp_path: Path) -> tuple[Keymap, UIActionBridge]:
    store = SQLiteStore(tmp_path / "shortcuts.sqlite")
    store.initialize()
    actions = ActionRegistry()
    actions.register(
        ActionDefinition(
            action_id="nav.tasks", label="Завдання", category="Навігація",
            default_binding="Ctrl+1",
        )
    )
    keymap = Keymap(store, actions)
    return keymap, UIActionBridge(actions, keymap)


@pytest.mark.parametrize(
    "bad",
    [
        "Ctrl+X\\nspoof".replace("\\n", "\n"),
        "Ctrl+\u202eX",
        "Ctrl+\x00X",
        "Ctrl+\ud800",
        "é" * 129,
    ],
)
def test_shortcut_control_input_is_never_persisted(
    tmp_path: Path, bad: str
) -> None:
    keymap, bridge = _keymap(tmp_path)
    with pytest.raises(ValueError):
        keymap.set_binding("nav.tasks", bad)
    assert keymap.resolve("nav.tasks") == "Ctrl+1"
    assert bridge.list_actions()[0]["binding"] == "Ctrl+1"


def test_import_rejects_unsafe_binding_atomically_and_allows_retry(tmp_path: Path) -> None:
    keymap, bridge = _keymap(tmp_path)
    malformed = json.dumps({
        "format_version": 1,
        "bindings": {"nav.tasks": "Ctrl+X\u202e"},
    })
    assert bridge.import_keymap(malformed)["ok"] is False
    assert keymap.resolve("nav.tasks") == "Ctrl+1"
    assert bridge.list_actions()[0]["binding"] == "Ctrl+1"
    keymap.import_json(json.dumps({
        "format_version": 1, "bindings": {"nav.tasks": "Ctrl+K"},
    }))
    assert keymap.resolve("nav.tasks") == "Ctrl+K"
    assert bridge.list_actions()[0]["binding"] == "Ctrl+K"


def _backend(tmp_path: Path) -> tuple[DesktopBackend, TaskQueue]:
    store = SQLiteStore(tmp_path / "tasks.sqlite")
    store.initialize()
    queue = TaskQueue(store)
    return DesktopBackend(
        queue=queue,
        agents=AgentRegistry(store),
        workspaces=WorkspaceRegistry(store),
        audit=AuditLog(store),
    ), queue


def _host_down() -> None:
    raise RuntimeError("runtime host unavailable")


def test_create_failure_never_leaves_orphan_ready_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, queue = _backend(tmp_path)
    monkeypatch.setattr(backend, "_host", _host_down)
    submitted: list[tuple[str, str]] = []
    try:
        with pytest.raises(RuntimeError, match="runtime host unavailable"):
            backend.create_task({"command": "safe command"})
        failed = queue.list_recent()[0]
        assert failed.state == TaskState.CANCELLED
        assert TaskQueue(queue.store).get(failed.task_id).state == TaskState.CANCELLED
        assert backend._active_futures == {}
        assert backend._runtime_loop is None

        # Once the runtime host returns, a fresh request is a new explicit
        # durable task; the previous failed admission cannot execute later.
        monkeypatch.setattr(
            backend, "_schedule_start",
            lambda task_id, command: submitted.append((task_id, command)),
        )
        assert backend.create_task({"command": "safe command"}).status == "accepted"
        ready = [task for task in queue.list_recent() if task.state == TaskState.READY]
        assert len(ready) == 1
        assert ready[0].task_id != failed.task_id
        assert submitted == [(ready[0].task_id, "safe command")]
    finally:
        backend.close()


def test_resume_host_failure_preserves_paused_restart_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend, queue = _backend(tmp_path)
    task = queue.create(
        workspace_id="default", agent_id="nika.default",
        payload={"command": "resume safely"},
    )
    queue.transition(task.task_id, TaskState.READY)
    queue.transition(task.task_id, TaskState.PAUSED)
    monkeypatch.setattr(backend, "_host", _host_down)
    started: list[tuple[str, str]] = []
    try:
        with pytest.raises(RuntimeError, match="runtime host unavailable"):
            backend.resume_task({"task_id": task.task_id})
        assert TaskQueue(queue.store).get(task.task_id).state == TaskState.PAUSED
        assert backend._active_futures == {}
        monkeypatch.setattr(
            backend, "_schedule_start",
            lambda task_id, command: started.append((task_id, command)),
        )
        assert backend.resume_task({"task_id": task.task_id}).status == "accepted"
        assert queue.get(task.task_id).state == TaskState.READY
        assert started == [(task.task_id, "resume safely")]
    finally:
        backend.close()
