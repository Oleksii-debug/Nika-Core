from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.agent_registry import AgentRegistry
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.kernel.workspace_registry import WorkspaceRegistry
from nika_core.ui.desktop_backend import DesktopBackend
from scripts import nika_windows


def _backend(tmp_path: Path) -> tuple[DesktopBackend, TaskQueue, SQLiteStore]:
    store = SQLiteStore(tmp_path / "full-state-ambiguity.db")
    store.initialize()
    queue = TaskQueue(store)
    backend = DesktopBackend(
        queue=queue,
        agents=AgentRegistry(store),
        workspaces=WorkspaceRegistry(store),
        audit=AuditLog(store),
    )
    return backend, queue, store


def _ready(queue: TaskQueue, command: str) -> str:
    record = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": command},
    )
    queue.transition(record.task_id, TaskState.READY)
    return record.task_id


def _paused(queue: TaskQueue, command: str) -> str:
    task_id = _ready(queue, command)
    queue.transition(task_id, TaskState.PAUSED)
    return task_id


def _terminal_churn(queue: TaskQueue, count: int = 50) -> None:
    for index in range(count):
        record = queue.create(
            workspace_id="default",
            agent_id="nika.default",
            payload={"command": f"terminal-{index}"},
        )
        queue.transition(record.task_id, TaskState.CANCELLED)


def _age_task(store: SQLiteStore, task_id: str) -> None:
    with store.connection() as conn:
        conn.execute(
            "UPDATE tasks SET created_at = ?, updated_at = ? WHERE task_id = ?",
            ("2000-01-01T00:00:00+00:00", "2000-01-01T00:00:00+00:00", task_id),
        )


def test_state_filter_finds_matching_tasks_outside_recent_window(tmp_path: Path) -> None:
    backend, queue, store = _backend(tmp_path)
    hidden = _ready(queue, "old ready")
    _age_task(store, hidden)
    _terminal_churn(queue)
    visible = _ready(queue, "new ready")

    assert hidden not in {record.task_id for record in queue.list_recent(limit=50)}
    matching = queue.list_by_states((TaskState.READY,), limit=2)

    assert {record.task_id for record in matching} == {hidden, visible}
    backend.close()


def test_unqualified_pause_rejects_hidden_second_active_task(tmp_path: Path) -> None:
    backend, queue, store = _backend(tmp_path)
    hidden = _ready(queue, "old ready")
    _age_task(store, hidden)
    _terminal_churn(queue)
    visible = _ready(queue, "new ready")

    with pytest.raises(ValueError, match="кілька завдань"):
        backend.pause_task({})

    assert queue.get(hidden).state is TaskState.READY
    assert queue.get(visible).state is TaskState.READY
    snapshot_ids = {item["task_id"] for item in backend.snapshot()["tasks"]}
    assert hidden in snapshot_ids
    assert visible in snapshot_ids
    backend.close()


def test_unqualified_resume_rejects_hidden_second_paused_task(tmp_path: Path) -> None:
    backend, queue, store = _backend(tmp_path)
    hidden = _paused(queue, "old paused")
    _age_task(store, hidden)
    _terminal_churn(queue)
    visible = _paused(queue, "new paused")

    with pytest.raises(ValueError, match="кілька завдань"):
        backend.resume_task({})

    assert queue.get(hidden).state is TaskState.PAUSED
    assert queue.get(visible).state is TaskState.PAUSED
    backend.close()


def test_current_status_rejects_hidden_second_unfinished_task(tmp_path: Path) -> None:
    backend, queue, store = _backend(tmp_path)
    hidden = _ready(queue, "old ready")
    _age_task(store, hidden)
    _terminal_churn(queue)
    visible = _ready(queue, "new ready")

    response = nika_windows._current_task_status_result(queue)

    assert response.status == "rejected"
    assert "кілька незавершених завдань" in response.message
    assert hidden not in response.message
    assert visible not in response.message
    backend.close()


def test_old_single_unfinished_task_remains_visible_and_reportable(tmp_path: Path) -> None:
    backend, queue, store = _backend(tmp_path)
    hidden = _ready(queue, "old only active")
    _age_task(store, hidden)
    _terminal_churn(queue)

    assert hidden not in {record.task_id for record in queue.list_recent(limit=50)}

    response = nika_windows._current_task_status_result(queue)
    snapshot_ids = {item["task_id"] for item in backend.snapshot()["tasks"]}

    assert response.status == "completed"
    assert hidden in response.message
    assert hidden in snapshot_ids
    backend.close()


def test_state_filter_validates_state_values_and_limit(tmp_path: Path) -> None:
    _backend_instance, queue, _store = _backend(tmp_path)

    with pytest.raises(TypeError, match="TaskState"):
        queue.list_by_states(("READY",), limit=2)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="between 1 and 500"):
        queue.list_by_states((TaskState.READY,), limit=0)

    _backend_instance.close()
