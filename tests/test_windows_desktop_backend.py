from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.action_registry import Keymap
from nika_core.kernel.agent_registry import AgentRegistry
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.default_actions import build_default_action_registry
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.kernel.workspace_registry import WorkspaceRegistry
from nika_core.ui.bridge import UIActionBridge
from nika_core.ui.desktop_backend import DesktopBackend


def build_backend(tmp_path: Path) -> tuple[DesktopBackend, TaskQueue, SQLiteStore]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    queue = TaskQueue(store)
    backend = DesktopBackend(
        queue=queue,
        agents=AgentRegistry(store),
        workspaces=WorkspaceRegistry(store),
        audit=AuditLog(store),
    )
    return backend, queue, store


def test_desktop_bootstrap_exposes_real_agent_and_workspace(tmp_path: Path) -> None:
    backend, _queue, _store = build_backend(tmp_path)
    snapshot = backend.snapshot()
    assert snapshot["agents"][0]["name"] == "Nika"
    assert snapshot["workspaces"][0]["name"] == "Основний"
    assert snapshot["tasks"] == []


def test_create_task_rejects_empty_command(tmp_path: Path) -> None:
    backend, _queue, _store = build_backend(tmp_path)
    with pytest.raises(ValueError, match="Введіть команду"):
        backend.create_task({"command": "   "})


def test_create_task_runs_real_no_llm_runtime_and_persists_result(tmp_path: Path) -> None:
    backend, queue, _store = build_backend(tmp_path)
    result = backend.create_task({"command": "перевір локальний стан"})
    assert result.status == "completed"
    tasks = queue.list_recent()
    assert len(tasks) == 1
    assert tasks[0].state == TaskState.COMPLETED
    assert tasks[0].payload["command"] == "перевір локальний стан"
    snapshot = backend.snapshot()
    assert snapshot["tasks"][0]["state"] == "COMPLETED"


def test_pause_resume_and_stop_use_persisted_task_state(tmp_path: Path) -> None:
    backend, queue, _store = build_backend(tmp_path)
    task = queue.create(workspace_id="default", agent_id="nika.default", payload={"command": "x"})
    queue.transition(task.task_id, TaskState.READY)
    assert backend.pause_task({}).status == "completed"
    assert queue.get(task.task_id).state == TaskState.PAUSED
    assert backend.resume_task({}).status == "completed"
    assert queue.get(task.task_id).state == TaskState.READY
    assert backend.stop_agent({}).status == "completed"
    assert queue.get(task.task_id).state == TaskState.CANCELLED


def test_bridge_returns_read_only_desktop_snapshot(tmp_path: Path) -> None:
    backend, _queue, store = build_backend(tmp_path)
    actions = build_default_action_registry()
    bridge = UIActionBridge(actions, Keymap(store, actions), state_provider=backend.snapshot)
    response = bridge.get_state()
    assert response["ok"] is True
    assert response["state"]["agents"][0]["agent_id"] == "nika.default"
