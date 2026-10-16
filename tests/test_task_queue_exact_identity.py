from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState


def _queue(tmp_path: Path) -> tuple[SQLiteStore, TaskQueue]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    return store, TaskQueue(store)


def test_exact_task_identity_is_idempotent_across_restart(tmp_path: Path) -> None:
    store, queue = _queue(tmp_path)
    payload = {
        "schema": "nika-product-factory-component-task-v1",
        "project_id": "project-a",
        "component_id": "component-api",
        "repository_id": "repo-a",
    }
    task_id = "product:project-a:component:component-api"

    created = queue.create_exact(
        task_id=task_id,
        workspace_id="product.factory",
        agent_id="product-factory-toolsmith",
        payload=payload,
    )
    replay = queue.create_exact(
        task_id=task_id,
        workspace_id="product.factory",
        agent_id="product-factory-toolsmith",
        payload=payload,
    )

    assert created == replay
    assert created.state is TaskState.CREATED
    with store.connection() as conn:
        event_count = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
    assert event_count == 1

    restarted = TaskQueue(SQLiteStore(store.path))
    restarted.store.initialize()
    assert restarted.create_exact(
        task_id=task_id,
        workspace_id="product.factory",
        agent_id="product-factory-toolsmith",
        payload=payload,
    ) == created


def test_exact_task_identity_conflict_fails_without_mutating_authority(tmp_path: Path) -> None:
    store, queue = _queue(tmp_path)
    task_id = "product:project-a:component:component-api"
    original_payload = {"project_id": "project-a", "component_id": "component-api"}
    original = queue.create_exact(
        task_id=task_id,
        workspace_id="product.factory",
        agent_id="product-factory-toolsmith",
        payload=original_payload,
    )

    with pytest.raises(ValueError, match="conflicts with existing task identity"):
        queue.create_exact(
            task_id=task_id,
            workspace_id="product.factory",
            agent_id="product-factory-toolsmith",
            payload={"project_id": "other-project", "component_id": "component-api"},
        )

    assert queue.get(task_id) == original
    with store.connection() as conn:
        event_count = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
    assert event_count == 1


def test_exact_task_identity_rejects_untrusted_shape(tmp_path: Path) -> None:
    _, queue = _queue(tmp_path)
    with pytest.raises(ValueError, match="task_id"):
        queue.create_exact(
            task_id=" product:project-a:component:component-api ",
            workspace_id="product.factory",
            agent_id="product-factory-toolsmith",
        )