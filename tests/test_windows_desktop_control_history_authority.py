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


def _build_backend(tmp_path: Path) -> tuple[DesktopBackend, TaskQueue]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    queue = TaskQueue(store)
    backend = DesktopBackend(
        queue=queue,
        agents=AgentRegistry(store),
        workspaces=WorkspaceRegistry(store),
        audit=AuditLog(store),
    )
    return backend, queue


def _create_task(queue: TaskQueue, *, command: str, state: TaskState) -> str:
    record = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": command},
    )
    queue.transition(record.task_id, TaskState.READY)
    if state == TaskState.READY:
        return record.task_id
    if state == TaskState.PAUSED:
        queue.transition(record.task_id, TaskState.PAUSED)
        return record.task_id
    if state == TaskState.COMPLETED:
        queue.transition(record.task_id, TaskState.RUNNING)
        queue.transition(record.task_id, TaskState.COMPLETED)
        return record.task_id
    raise AssertionError(f"unsupported fixture state: {state.value}")


def _seed_hidden_ambiguity(
    queue: TaskQueue,
    *,
    eligible_state: TaskState,
) -> tuple[str, str]:
    older_id = _create_task(queue, command="older eligible", state=eligible_state)
    for index in range(50):
        _create_task(
            queue,
            command=f"completed filler {index}",
            state=TaskState.COMPLETED,
        )
    newer_id = _create_task(queue, command="newer eligible", state=eligible_state)
    return older_id, newer_id


@pytest.mark.parametrize(
    ("method_name", "eligible_state", "error_fragment"),
    [
        ("pause_task", TaskState.READY, "кілька завдань, доступних для призупинення"),
        ("resume_task", TaskState.PAUSED, "кілька завдань, доступних для продовження"),
        ("stop_agent", TaskState.READY, "кілька завдань, доступних для зупинки"),
    ],
)
def test_unqualified_desktop_controls_detect_eligible_task_beyond_recent_snapshot(
    tmp_path: Path,
    method_name: str,
    eligible_state: TaskState,
    error_fragment: str,
) -> None:
    backend, queue = _build_backend(tmp_path)
    older_id, newer_id = _seed_hidden_ambiguity(queue, eligible_state=eligible_state)

    recent_ids = {record.task_id for record in queue.list_recent(limit=50)}
    assert newer_id in recent_ids
    assert older_id not in recent_ids

    with pytest.raises(ValueError, match=error_fragment):
        getattr(backend, method_name)({})

    assert queue.get(older_id).state == eligible_state
    assert queue.get(newer_id).state == eligible_state
    backend.close()
