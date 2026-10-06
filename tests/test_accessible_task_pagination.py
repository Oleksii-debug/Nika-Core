from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.agent_registry import AgentRegistry
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.default_actions import build_default_action_registry
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.kernel.workspace_registry import WorkspaceRegistry
from nika_core.ui.desktop_backend import DesktopBackend
from scripts import nika_windows


def _backend(tmp_path: Path) -> tuple[DesktopBackend, TaskQueue]:
    store = SQLiteStore(tmp_path / "task-pages.db")
    store.initialize()
    queue = TaskQueue(store)
    backend = DesktopBackend(
        queue=queue,
        agents=AgentRegistry(store),
        workspaces=WorkspaceRegistry(store),
        audit=AuditLog(store),
    )
    return backend, queue


def _ready(queue: TaskQueue, index: int) -> str:
    record = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": f"task-{index}"},
    )
    queue.transition(record.task_id, TaskState.READY)
    return record.task_id


def test_task_state_filter_supports_bounded_offset_and_validates_it(tmp_path: Path) -> None:
    backend, queue = _backend(tmp_path)
    task_ids = [_ready(queue, index) for index in range(55)]

    first = queue.list_by_states((TaskState.READY,), limit=50, offset=0)
    second = queue.list_by_states((TaskState.READY,), limit=50, offset=50)

    assert len(first) == 50
    assert len(second) == 5
    assert {item.task_id for item in (*first, *second)} == set(task_ids)
    with pytest.raises(ValueError, match="non-negative integer"):
        queue.list_by_states((TaskState.READY,), offset=-1)
    with pytest.raises(ValueError, match="non-negative integer"):
        queue.list_by_states((TaskState.READY,), offset=True)  # type: ignore[arg-type]
    backend.close()


def test_later_task_page_fails_closed_on_noncanonical_durable_state(
    tmp_path: Path,
) -> None:
    backend, queue = _backend(tmp_path)
    task_ids = [_ready(queue, index) for index in range(55)]
    with queue.store.connection() as conn:
        conn.execute(
            "UPDATE tasks SET state = ? WHERE task_id = ?",
            ("UNKNOWN_STATE", task_ids[0]),
        )

    with pytest.raises(ValueError, match="UNKNOWN_STATE"):
        queue.list_by_states((TaskState.READY,), limit=50, offset=50)
    backend.close()


def test_task_state_pages_have_deterministic_tie_order(tmp_path: Path) -> None:
    backend, queue = _backend(tmp_path)
    task_ids = [_ready(queue, index) for index in range(55)]
    with queue.store.connection() as conn:
        conn.execute(
            "UPDATE tasks SET created_at = ?, updated_at = ?",
            ("2026-10-06T00:00:00+00:00", "2026-10-06T00:00:00+00:00"),
        )

    first = queue.list_by_states((TaskState.READY,), limit=50, offset=0)
    second = queue.list_by_states((TaskState.READY,), limit=50, offset=50)

    assert [item.task_id for item in (*first, *second)] == sorted(task_ids, reverse=True)
    backend.close()


def test_backend_pages_all_unfinished_task_ids_without_unbounded_snapshot(
    tmp_path: Path,
) -> None:
    backend, queue = _backend(tmp_path)
    task_ids = {_ready(queue, index) for index in range(55)}

    first = backend.snapshot()
    first_ids = {item["task_id"] for item in first["tasks"]}

    assert len(first_ids) == 50
    assert first["task_page"] == {
        "schema": "nika.task-page:v1",
        "page_size": 50,
        "offset": 0,
        "page_number": 1,
        "has_previous": False,
        "has_next": True,
        "unfinished_only": True,
    }

    advanced = backend.next_task_page({})
    second = backend.snapshot()
    second_ids = {item["task_id"] for item in second["tasks"]}

    assert advanced.status == "completed"
    assert advanced.focus_id == "tasks-heading"
    assert len(second_ids) == 5
    assert first_ids.isdisjoint(second_ids)
    assert first_ids | second_ids == task_ids
    assert second["task_page"]["offset"] == 50
    assert second["task_page"]["page_number"] == 2
    assert second["task_page"]["has_previous"] is True
    assert second["task_page"]["has_next"] is False
    assert second["task_page"]["unfinished_only"] is True

    terminal = backend.next_task_page({})
    assert terminal.message == "Це остання сторінка незавершених завдань."
    assert backend.snapshot()["task_page"]["offset"] == 50

    back = backend.previous_task_page({})
    assert back.status == "completed"
    assert backend.snapshot()["task_page"]["offset"] == 0
    backend.close()


def test_task_page_resets_if_live_churn_removes_the_current_window(tmp_path: Path) -> None:
    backend, queue = _backend(tmp_path)
    ids = [_ready(queue, index) for index in range(51)]
    backend.next_task_page({})
    assert backend.snapshot()["task_page"]["offset"] == 50

    for task_id in ids:
        queue.transition(task_id, TaskState.CANCELLED)

    snapshot = backend.snapshot()

    assert snapshot["task_page"]["offset"] == 0
    assert snapshot["task_page"]["has_previous"] is False
    assert snapshot["task_page"]["has_next"] is False
    assert snapshot["task_page"]["unfinished_only"] is False
    backend.close()


def test_bridge_and_html_expose_keyboard_reachable_task_page_controls(tmp_path: Path) -> None:
    database = (tmp_path / "task-page-bridge.db").resolve()
    bridge, _products = nika_windows.build_windows_bridge(
        AppConfig(database_path=database),
        start_startup_recovery=False,
    )
    store = SQLiteStore(database)
    store.initialize()
    queue = TaskQueue(store)
    task_ids = {_ready(queue, index) for index in range(55)}

    first = bridge.get_state()
    assert first["ok"] is True
    assert first["state"]["task_page"]["has_next"] is True

    moved = bridge.dispatch(
        {
            "request_id": "tasks-next",
            "action_id": "task.page.next",
            "payload": {},
        }
    )
    second = bridge.get_state()

    assert moved["status"] == "completed"
    assert moved["focus_id"] == "tasks-heading"
    assert second["ok"] is True
    assert second["state"]["task_page"]["offset"] == 50
    assert {item["task_id"] for item in second["state"]["tasks"]} <= task_ids

    actions = {item.action_id for item in build_default_action_registry().all()}
    assert {"task.page.previous", "task.page.next"} <= actions

    root = Path(__file__).resolve().parents[1]
    html = (root / "src/nika_core/ui/web/index.html").read_text(encoding="utf-8")
    script = (root / "src/nika_core/ui/web/app.js").read_text(encoding="utf-8")
    assert 'id="tasks-page-previous"' in html
    assert 'data-action-id="task.page.previous"' in html
    assert 'id="tasks-page-next"' in html
    assert 'data-action-id="task.page.next"' in html
    assert 'function renderTaskPage(snapshot)' in script
    assert 'state.task_page ?? null' in script


def test_task_page_actions_reject_payload_authority(tmp_path: Path) -> None:
    backend, _queue = _backend(tmp_path)

    with pytest.raises(ValueError, match="does not accept payload authority"):
        backend.next_task_page({"offset": 500})
    with pytest.raises(TypeError, match="exact dict"):
        backend.previous_task_page([])  # type: ignore[arg-type]

    backend.close()
