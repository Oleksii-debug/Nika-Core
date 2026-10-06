from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.agent_registry import AgentRegistry
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.kernel.workspace_registry import WorkspaceRegistry
from nika_core.product_command.product_project_adapter import ProductProjectCommandService
from nika_core.product_factory_packaged_journey import (
    PackagedProductCommandRouter,
    PackagedProductJourneyError,
    packaged_task_direct_action,
    packaged_task_direct_target,
)
from nika_core.product_project import ProductProjectRepository
from nika_core.runtime.contracts import (
    RuntimeCapability,
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResult,
    RuntimeResumeRequest,
    RuntimeUnsupportedError,
)
from nika_core.ui.bridge_models import UIResult
from nika_core.ui.desktop_backend import DesktopBackend
from scripts import nika_windows

ROOT = Path(__file__).parents[1]


class _MultiTaskRuntime:
    runtime_id = "targeted-task-control-test"
    capabilities = frozenset({RuntimeCapability.CANCELLATION})

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started: set[str] = set()
        self._cancelled: set[str] = set()
        self.release = threading.Event()

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        with self._lock:
            self._started.add(request.task_id)
        while True:
            with self._lock:
                if request.task_id in self._cancelled:
                    return RuntimeResult(outcome=RuntimeOutcome.CANCELLED)
            if self.release.is_set():
                return RuntimeResult(outcome=RuntimeOutcome.COMPLETED)
            await asyncio.sleep(0.01)

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        raise RuntimeUnsupportedError(f"resume unsupported for {request.task_id}")

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        del thread_id
        with self._lock:
            if task_id not in self._started or task_id in self._cancelled:
                return False
            self._cancelled.add(task_id)
        return True

    def wait_started(self, count: int, *, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if len(self._started) >= count:
                    return True
            time.sleep(0.01)
        return False

    def cancelled_ids(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._cancelled)


def _ready(queue: TaskQueue, command: str) -> str:
    record = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": command},
    )
    queue.transition(record.task_id, TaskState.READY)
    return record.task_id


def _backend(tmp_path: Path) -> tuple[DesktopBackend, TaskQueue, SQLiteStore]:
    store = SQLiteStore(tmp_path / "targeted-controls.db")
    store.initialize()
    queue = TaskQueue(store)
    backend = DesktopBackend(
        queue=queue,
        agents=AgentRegistry(store),
        workspaces=WorkspaceRegistry(store),
        audit=AuditLog(store),
    )
    return backend, queue, store


def _wait_for_state(
    queue: TaskQueue,
    task_id: str,
    expected: TaskState,
    *,
    timeout: float = 2.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if queue.get(task_id).state is expected:
            return
        time.sleep(0.01)
    assert queue.get(task_id).state is expected


@pytest.mark.parametrize(
    ("command", "action"),
    (
        ("pause task {task_id}", "pause"),
        ("resume task {task_id}", "resume"),
        ("stop task {task_id}", "stop"),
        ("task status {task_id}", "status"),
        ("current task status {task_id}", "status"),
        ("призупини завдання {task_id}", "pause"),
        ("продовж завдання {task_id}", "resume"),
        ("зупини завдання {task_id}", "stop"),
        ("статус завдання {task_id}", "status"),
    ),
)
def test_targeted_direct_parser_accepts_canonical_uuid(
    command: str,
    action: str,
) -> None:
    task_id = "123e4567-e89b-42d3-a456-426614174000"
    rendered = command.format(task_id=task_id)

    assert packaged_task_direct_target(rendered) == (action, task_id)
    assert packaged_task_direct_action(rendered) == action


@pytest.mark.parametrize(
    "command",
    (
        "pause task not-a-uuid",
        "resume task 123E4567-E89B-42D3-A456-426614174000",
        "stop task 123e4567e89b42d3a456426614174000",
        "статус завдання 123",
    ),
)
def test_targeted_direct_parser_rejects_noncanonical_task_id(command: str) -> None:
    with pytest.raises(PackagedProductJourneyError, match="UUID"):
        packaged_task_direct_target(command)


def test_targeted_direct_parser_does_not_capture_multword_general_request() -> None:
    assert packaged_task_direct_target("pause task execution safely") is None
    assert packaged_task_direct_target(
        "продовж завдання після перевірки користувачем"
    ) is None


def test_router_forwards_only_canonical_target_id(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "router.db")
    store.initialize()
    calls: list[tuple[str, Mapping[str, Any]]] = []
    statuses: list[str | None] = []

    def ordinary(_payload: Mapping[str, Any]) -> UIResult:
        raise AssertionError("targeted task control must not create an ordinary task")

    def control(payload: Mapping[str, Any]) -> UIResult:
        calls.append(("pause", payload))
        return UIResult(
            request_id="desktop-handler",
            status="completed",
            message="paused",
            focus_id="tasks-heading",
        )

    def status(task_id: str | None) -> UIResult:
        statuses.append(task_id)
        return UIResult(
            request_id="desktop-handler",
            status="completed",
            message="status",
            focus_id="tasks-heading",
        )

    router = PackagedProductCommandRouter(
        products=ProductProjectCommandService(ProductProjectRepository(store)),
        ordinary_handler=ordinary,
        task_pause_handler=control,
        task_status_handler=status,
    )
    task_id = "123e4567-e89b-42d3-a456-426614174000"

    router.create(
        {
            "command": f"pause task {task_id}",
            "private": "must-not-cross-boundary",
        }
    )
    router.create({"command": f"task status {task_id}"})

    assert calls == [("pause", {"task_id": task_id})]
    assert statuses == [task_id]


def test_backend_targeted_pause_resolves_ambiguous_ready_tasks(tmp_path: Path) -> None:
    backend, queue, _store = _backend(tmp_path)
    first = _ready(queue, "first")
    second = _ready(queue, "second")

    result = backend.pause_task({"task_id": first})

    assert result.status == "completed"
    assert queue.get(first).state is TaskState.PAUSED
    assert queue.get(second).state is TaskState.READY
    backend.close()


def test_backend_targeted_resume_resolves_ambiguous_paused_tasks(tmp_path: Path) -> None:
    backend, queue, _store = _backend(tmp_path)
    first = _ready(queue, "first")
    second = _ready(queue, "second")
    backend.pause_task({"task_id": first})
    backend.pause_task({"task_id": second})

    result = backend.resume_task({"task_id": first})

    assert result.status == "accepted"
    _wait_for_state(queue, first, TaskState.COMPLETED)
    assert queue.get(second).state is TaskState.PAUSED
    backend.close()


def test_backend_targeted_stop_resolves_ambiguous_ready_tasks(tmp_path: Path) -> None:
    backend, queue, _store = _backend(tmp_path)
    first = _ready(queue, "first")
    second = _ready(queue, "second")

    result = backend.stop_agent({"task_id": second})

    assert result.status == "completed"
    assert queue.get(first).state is TaskState.READY
    assert queue.get(second).state is TaskState.CANCELLED
    backend.close()


def test_backend_targeted_stop_cancels_only_selected_live_runtime(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "targeted-live-stop.db")
    store.initialize()
    queue = TaskQueue(store)
    runtime = _MultiTaskRuntime()
    backend = DesktopBackend(
        queue=queue,
        agents=AgentRegistry(store),
        workspaces=WorkspaceRegistry(store),
        audit=AuditLog(store),
        runtime=runtime,
    )
    backend.create_task({"command": "first live"})
    backend.create_task({"command": "second live"})
    assert runtime.wait_started(2)
    records = {
        str(record.payload["command"]): record
        for record in queue.list_recent(limit=10)
    }
    first = records["first live"]
    second = records["second live"]
    _wait_for_state(queue, first.task_id, TaskState.RUNNING)
    _wait_for_state(queue, second.task_id, TaskState.RUNNING)

    try:
        result = backend.stop_agent({"task_id": first.task_id})

        assert result.status == "accepted"
        _wait_for_state(queue, first.task_id, TaskState.CANCELLED)
        assert queue.get(second.task_id).state is TaskState.RUNNING
        assert runtime.cancelled_ids() == frozenset({first.task_id})
    finally:
        runtime.release.set()
        _wait_for_state(queue, second.task_id, TaskState.COMPLETED)
        backend.close()


def test_backend_targeted_control_rejects_unknown_and_malformed_ids(tmp_path: Path) -> None:
    backend, queue, _store = _backend(tmp_path)
    existing = _ready(queue, "preserve me")
    unknown = str(uuid4())

    with pytest.raises(ValueError, match="не знайдено"):
        backend.pause_task({"task_id": unknown})
    with pytest.raises(ValueError, match="UUID"):
        backend.stop_agent({"task_id": "not-a-uuid"})

    assert queue.get(existing).state is TaskState.READY
    backend.close()


def test_targeted_status_selects_one_task_without_payload_disclosure(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "status.db")
    store.initialize()
    queue = TaskQueue(store)
    first = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "private first command", "credential": "SECRET-A"},
    )
    queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "private second command", "credential": "SECRET-B"},
    )

    response = nika_windows._current_task_status_result(queue, first.task_id)

    assert response.status == "completed"
    assert first.task_id in response.message
    assert TaskState.CREATED.value in response.message
    assert "private first command" not in response.message
    assert "SECRET-A" not in response.message
    assert "SECRET-B" not in response.message


def test_windows_bridge_targets_one_of_multiple_tasks_by_id(tmp_path: Path) -> None:
    database = (tmp_path / "targeted windows bridge.db").resolve()
    bridge, _products = nika_windows.build_windows_bridge(
        AppConfig(database_path=database),
        start_startup_recovery=False,
    )
    store = SQLiteStore(database)
    store.initialize()
    queue = TaskQueue(store)
    first = _ready(queue, "first")
    second = _ready(queue, "second")

    paused = bridge.dispatch(
        {
            "request_id": "targeted-pause",
            "action_id": "task.create",
            "payload": {"command": f"pause task {first}"},
        }
    )
    status = bridge.dispatch(
        {
            "request_id": "targeted-status",
            "action_id": "task.create",
            "payload": {"command": f"task status {first}"},
        }
    )
    stopped = bridge.dispatch(
        {
            "request_id": "targeted-stop",
            "action_id": "task.create",
            "payload": {"command": f"stop task {second}"},
        }
    )

    assert paused["status"] == "completed"
    assert status["status"] == "completed"
    assert first in status["message"]
    assert stopped["status"] == "completed"
    assert queue.get(first).state is TaskState.PAUSED
    assert queue.get(second).state is TaskState.CANCELLED
    assert len(queue.list_recent()) == 2


def test_packaged_assets_expose_task_ids_and_keyboard_command_help() -> None:
    html = (ROOT / "src/nika_core/ui/web/index.html").read_text(encoding="utf-8")
    app = (ROOT / "src/nika_core/ui/web/app.js").read_text(encoding="utf-8")

    assert 'aria-describedby="execution-mode task-control-help"' in html
    assert 'id="task-control-help"' in html
    assert "pause task &lt;task_id&gt;" in html
    assert "статус завдання &lt;task_id&gt;" in html
    assert 'ID: ${item.task_id}' in app
