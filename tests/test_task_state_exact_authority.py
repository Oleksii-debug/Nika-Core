from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState, can_transition, require_transition


class _ForgedReady(str):
    @property
    def value(self) -> str:
        return TaskState.COMPLETED.value


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    return store


def test_transition_predicate_rejects_noncanonical_task_state_carriers() -> None:
    forged = _ForgedReady(TaskState.READY.value)

    assert forged == TaskState.READY
    assert forged.value == TaskState.COMPLETED.value
    assert can_transition(TaskState.CREATED, TaskState.READY) is True
    assert can_transition(TaskState.CREATED, forged) is False  # type: ignore[arg-type]
    assert (
        can_transition(TaskState.CREATED, TaskState.READY.value) is False  # type: ignore[arg-type]
    )
    assert (
        can_transition(TaskState.CREATED.value, TaskState.READY) is False  # type: ignore[arg-type]
    )

    with pytest.raises(TypeError, match="target must be an exact TaskState"):
        require_transition(TaskState.CREATED, forged)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="current must be an exact TaskState"):
        require_transition(TaskState.CREATED.value, TaskState.READY)  # type: ignore[arg-type]


def test_forged_ready_cannot_persist_a_different_task_state(tmp_path: Path) -> None:
    store = _store(tmp_path)
    queue = TaskQueue(store)
    task = queue.create(workspace_id="workspace", agent_id="agent")
    forged = _ForgedReady(TaskState.READY.value)

    with pytest.raises(TypeError, match="target must be an exact TaskState"):
        queue.transition(task.task_id, forged)  # type: ignore[arg-type]

    assert queue.get(task.task_id).state is TaskState.CREATED
    with store.connection() as conn:
        rows = conn.execute(
            "SELECT previous_state, new_state FROM task_events "
            "WHERE task_id = ? ORDER BY event_id",
            (task.task_id,),
        ).fetchall()
    assert [(row["previous_state"], row["new_state"]) for row in rows] == [
        (None, TaskState.CREATED.value)
    ]
