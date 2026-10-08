from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.product_command.product_project_adapter import ProductProjectCommandService
from nika_core.product_factory_packaged_journey import (
    PackagedProductCommandRouter,
    PackagedProductJourneyError,
    packaged_task_direct_action,
    product_project_identity,
)
from nika_core.product_project import ProductProjectRepository
from nika_core.ui.bridge_models import UIResult
from scripts import nika_windows


class _OrdinaryHandler:
    def __init__(self) -> None:
        self.calls: list[Mapping[str, Any]] = []

    def __call__(self, payload: Mapping[str, Any]) -> UIResult:
        self.calls.append(payload)
        return UIResult(
            request_id="desktop-handler",
            status="completed",
            message="ordinary",
            focus_id="tasks-heading",
        )


def _result(message: str) -> UIResult:
    return UIResult(
        request_id="desktop-handler",
        status="completed",
        message=message,
        focus_id="tasks-heading",
    )


def _router(
    path: Path,
    *,
    calls: list[tuple[str, Mapping[str, Any]]] | None = None,
    include_status: bool = True,
) -> tuple[PackagedProductCommandRouter, _OrdinaryHandler]:
    store = SQLiteStore(path)
    store.initialize()
    ordinary = _OrdinaryHandler()
    recorded = calls if calls is not None else []

    def control(name: str):
        def handler(payload: Mapping[str, Any]) -> UIResult:
            recorded.append((name, payload))
            return _result(name)

        return handler

    router = PackagedProductCommandRouter(
        products=ProductProjectCommandService(ProductProjectRepository(store)),
        ordinary_handler=ordinary,
        task_pause_handler=control("pause"),
        task_resume_handler=control("resume"),
        task_stop_handler=control("stop"),
        task_status_handler=(lambda _task_id: _result("status")) if include_status else None,
    )
    return router, ordinary


@pytest.mark.parametrize(
    ("command", "action"),
    (
        ("pause task", "pause"),
        ("Pause current task!", "pause"),
        ("призупини поточне завдання", "pause"),
        ("resume task", "resume"),
        ("continue task", "resume"),
        ("продовжити завдання", "resume"),
        ("stop task", "stop"),
        ("cancel task", "stop"),
        ("скасуй завдання", "stop"),
        ("current task", "status"),
        ("show current task", "status"),
        ("статус поточного завдання", "status"),
    ),
)
def test_task_direct_action_recognizes_only_exact_aliases(
    command: str,
    action: str,
) -> None:
    assert packaged_task_direct_action(command) == action


@pytest.mark.parametrize(
    "command",
    (
        "Explain how to pause task execution safely",
        "Write a report about current task status",
        "Поясни, як призупинити завдання без втрати даних",
        "Створи застосунок для відстеження статусу завдань",
    ),
)
def test_task_direct_action_does_not_capture_general_language(command: str) -> None:
    assert packaged_task_direct_action(command) is None


@pytest.mark.parametrize(
    ("command", "expected"),
    (
        ("pause task", "pause"),
        ("resume task", "resume"),
        ("stop task", "stop"),
    ),
)
def test_direct_task_mutations_delegate_without_forwarding_command_payload(
    tmp_path: Path,
    command: str,
    expected: str,
) -> None:
    calls: list[tuple[str, Mapping[str, Any]]] = []
    router, ordinary = _router(tmp_path / f"{expected}.db", calls=calls)

    response = router.create(
        {
            "command": command,
            "unrelated": "must-not-cross-task-control-boundary",
        }
    )

    assert response.message == expected
    assert calls == [(expected, {})]
    assert ordinary.calls == []


def test_direct_task_status_uses_injected_read_only_handler(tmp_path: Path) -> None:
    router, ordinary = _router(tmp_path / "status.db")

    response = router.create({"command": "покажи поточне завдання"})

    assert response.message == "status"
    assert ordinary.calls == []


def test_missing_direct_task_handler_fails_closed_before_ordinary_task(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "missing-handler.db")
    store.initialize()
    ordinary = _OrdinaryHandler()
    router = PackagedProductCommandRouter(
        products=ProductProjectCommandService(ProductProjectRepository(store)),
        ordinary_handler=ordinary,
    )

    with pytest.raises(PackagedProductJourneyError, match="недоступне"):
        router.create({"command": "pause task"})

    assert ordinary.calls == []


def test_task_direct_command_preserves_product_project_selection(tmp_path: Path) -> None:
    router, ordinary = _router(tmp_path / "selection.db")
    product_command = "Create an accessible Windows application for expense tracking"
    product_id = product_project_identity(product_command)
    router.create({"command": product_command})

    status = router.create({"command": "current task"})

    assert status.message == "status"
    assert router.active_project_id == product_id
    assert ordinary.calls == []


def test_current_task_status_is_bounded_and_omits_payload_details(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "bounded.db")
    store.initialize()
    queue = TaskQueue(store)
    record = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "private command", "credential": "TOP-SECRET"},
    )

    response = nika_windows._current_task_status_result(queue)

    assert response.status == "completed"
    assert record.task_id in response.message
    assert TaskState.CREATED.value in response.message
    assert "private command" not in response.message
    assert "TOP-SECRET" not in response.message
    assert response.focus_id == "tasks-heading"


def test_current_task_status_rejects_ambiguous_multiple_unfinished_tasks(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "ambiguous.db")
    store.initialize()
    queue = TaskQueue(store)
    first = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "one"},
    )
    second = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "two"},
    )

    response = nika_windows._current_task_status_result(queue)

    assert response.status == "rejected"
    assert "кілька незавершених завдань" in response.message
    assert first.task_id not in response.message
    assert second.task_id not in response.message


def test_current_task_status_fails_closed_on_corrupt_durable_payload(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "corrupt.db")
    store.initialize()
    queue = TaskQueue(store)
    record = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "safe"},
    )
    with store.connection() as conn:
        conn.execute(
            "UPDATE tasks SET payload_json = ? WHERE task_id = ?",
            ('{"command":"one","command":"two"}', record.task_id),
        )

    response = nika_windows._current_task_status_result(queue)

    assert response.status == "failed"
    assert "one" not in response.message
    assert "two" not in response.message
    assert response.focus_id == "tasks-heading"


def test_current_task_status_reports_absence_without_creating_work(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "empty.db")
    store.initialize()
    queue = TaskQueue(store)

    response = nika_windows._current_task_status_result(queue)

    assert response.status == "completed"
    assert response.message == "Немає незавершеного завдання."
    assert queue.list_recent() == ()


def test_current_windows_bridge_routes_status_pause_and_stop_without_new_task(
    tmp_path: Path,
) -> None:
    database = (tmp_path / "windows task controls.db").resolve()
    bridge, _products = nika_windows.build_windows_bridge(
        AppConfig(database_path=database),
        start_startup_recovery=False,
    )
    store = SQLiteStore(database)
    store.initialize()
    queue = TaskQueue(store)
    record = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "fixture command"},
    )
    queue.transition(record.task_id, TaskState.READY)

    status = bridge.dispatch(
        {
            "request_id": "task-status",
            "action_id": "task.create",
            "payload": {"command": "current task"},
        }
    )
    paused = bridge.dispatch(
        {
            "request_id": "task-pause",
            "action_id": "task.create",
            "payload": {"command": "pause task"},
        }
    )
    stopped = bridge.dispatch(
        {
            "request_id": "task-stop",
            "action_id": "task.create",
            "payload": {"command": "stop task"},
        }
    )

    assert status["status"] == "completed"
    assert record.task_id in status["message"]
    assert paused["status"] == "completed"
    assert stopped["status"] == "completed"
    assert TaskQueue(store).get(record.task_id).state is TaskState.CANCELLED
    assert len(TaskQueue(store).list_recent()) == 1
