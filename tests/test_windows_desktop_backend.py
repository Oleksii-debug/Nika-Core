import asyncio
import threading
import time
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
from nika_core.runtime.contracts import (
    AgentRuntimePort,
    RuntimeCapability,
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResult,
    RuntimeResumeRequest,
    RuntimeUnsupportedError,
)
from nika_core.runtime.session_store import RuntimeSessionStore
from nika_core.ui.bridge import UIActionBridge
from nika_core.ui.desktop_backend import DesktopBackend


def build_backend(
    tmp_path: Path,
    *,
    runtime: AgentRuntimePort | None = None,
) -> tuple[DesktopBackend, TaskQueue, SQLiteStore]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    queue = TaskQueue(store)
    backend = DesktopBackend(
        queue=queue,
        agents=AgentRegistry(store),
        workspaces=WorkspaceRegistry(store),
        audit=AuditLog(store),
        runtime=runtime,
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
    pytest.fail(
        f"task {task_id} did not reach {expected.value}; "
        f"current={queue.get(task_id).state.value}"
    )


class BlockingRuntime:
    runtime_id = "desktop-blocking"
    capabilities = frozenset({RuntimeCapability.CANCELLATION})

    def __init__(self) -> None:
        self.started = threading.Event()
        self.cancel_requested = threading.Event()
        self._cancel_event: asyncio.Event | None = None
        self._task_id: str | None = None
        self._thread_id: str | None = None

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        self._task_id = request.task_id
        self._thread_id = request.thread_id
        self._cancel_event = asyncio.Event()
        self.started.set()
        await self._cancel_event.wait()
        return RuntimeResult(outcome=RuntimeOutcome.CANCELLED)

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        raise RuntimeUnsupportedError(f"resume is not supported for {request.task_id}")

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        if (
            task_id != self._task_id
            or thread_id != self._thread_id
            or self._cancel_event is None
        ):
            return False
        self.cancel_requested.set()
        self._cancel_event.set()
        return True


class PausingRuntime:
    runtime_id = "desktop-pausing"
    capabilities = frozenset({RuntimeCapability.DURABLE_RESUME})

    def __init__(self) -> None:
        self.resumed = threading.Event()

    def initial_resume_token(self, *, task_id: str, thread_id: str) -> str:
        return f"initial:{task_id}:{thread_id}"

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        return RuntimeResult(
            outcome=RuntimeOutcome.PAUSED,
            resume_token=f"resume:{request.task_id}",
        )

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        self.resumed.set()
        return RuntimeResult(outcome=RuntimeOutcome.COMPLETED)

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        return False


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


def test_create_task_dispatches_no_llm_runtime_without_blocking_bridge(tmp_path: Path) -> None:
    backend, queue, _store = build_backend(tmp_path)
    try:
        result = backend.create_task({"command": "перевір локальний стан"})
        assert result.status == "accepted"
        tasks = queue.list_recent()
        assert len(tasks) == 1
        assert tasks[0].payload["command"] == "перевір локальний стан"
        _wait_for_state(queue, tasks[0].task_id, TaskState.COMPLETED)
        snapshot = backend.snapshot()
        assert snapshot["tasks"][0]["state"] == "COMPLETED"
    finally:
        backend.close()


def test_stop_immediately_after_acceptance_routes_through_runtime_cancel(tmp_path: Path) -> None:
    runtime = BlockingRuntime()
    backend, queue, _store = build_backend(tmp_path, runtime=runtime)
    try:
        result = backend.create_task({"command": "скасуй одразу"})
        assert result.status == "accepted"
        task = queue.list_recent()[0]

        stop = backend.stop_agent({})
        assert stop.status == "accepted"
        assert runtime.started.wait(timeout=1.0)
        assert runtime.cancel_requested.wait(timeout=1.0)
        _wait_for_state(queue, task.task_id, TaskState.CANCELLED)
    finally:
        backend.close()


def test_running_runtime_keeps_stop_available_and_pause_fail_closed(tmp_path: Path) -> None:
    runtime = BlockingRuntime()
    backend, queue, _store = build_backend(tmp_path, runtime=runtime)
    try:
        result = backend.create_task({"command": "довге локальне завдання"})
        assert result.status == "accepted"
        assert runtime.started.wait(timeout=1.0)
        task = queue.list_recent()[0]
        _wait_for_state(queue, task.task_id, TaskState.RUNNING)

        with pytest.raises(ValueError, match="не підтримує зовнішній pause"):
            backend.pause_task({})
        assert queue.get(task.task_id).state is TaskState.RUNNING

        stop = backend.stop_agent({})
        assert stop.status == "accepted"
        assert runtime.cancel_requested.wait(timeout=1.0)
        _wait_for_state(queue, task.task_id, TaskState.CANCELLED)
    finally:
        backend.close()


def test_runtime_paused_task_resumes_through_durable_session(tmp_path: Path) -> None:
    runtime = PausingRuntime()
    backend, queue, store = build_backend(tmp_path, runtime=runtime)
    try:
        result = backend.create_task({"command": "зупинись і продовж"})
        assert result.status == "accepted"
        task = queue.list_recent()[0]
        _wait_for_state(queue, task.task_id, TaskState.PAUSED)
        session = RuntimeSessionStore(store).get(task.task_id)
        assert session is not None
        assert session.runtime_id == runtime.runtime_id

        resumed = backend.resume_task({})
        assert resumed.status == "accepted"
        assert runtime.resumed.wait(timeout=1.0)
        _wait_for_state(queue, task.task_id, TaskState.COMPLETED)
    finally:
        backend.close()


def test_pause_resume_and_stop_use_persisted_task_state(tmp_path: Path) -> None:
    backend, queue, _store = build_backend(tmp_path)
    task = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "x"},
    )
    queue.transition(task.task_id, TaskState.READY)
    assert backend.pause_task({}).status == "completed"
    assert queue.get(task.task_id).state == TaskState.PAUSED
    assert backend.resume_task({}).status == "completed"
    assert queue.get(task.task_id).state == TaskState.READY
    assert backend.stop_agent({}).status == "completed"
    assert queue.get(task.task_id).state == TaskState.CANCELLED


def test_resume_started_paused_task_without_session_fails_closed(tmp_path: Path) -> None:
    backend, queue, _store = build_backend(tmp_path)
    task = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "x"},
    )
    queue.transition(task.task_id, TaskState.READY)
    queue.transition(task.task_id, TaskState.RUNNING)
    queue.transition(task.task_id, TaskState.PAUSED)

    with pytest.raises(ValueError, match="не має збереженої runtime-сесії"):
        backend.resume_task({})

    assert queue.get(task.task_id).state is TaskState.PAUSED
    backend.close()


def test_stop_fails_closed_for_running_task_without_runtime_session(tmp_path: Path) -> None:
    backend, queue, _store = build_backend(tmp_path)
    task = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "x"},
    )
    queue.transition(task.task_id, TaskState.READY)
    queue.transition(task.task_id, TaskState.RUNNING)

    with pytest.raises(ValueError, match="без збереженої"):
        backend.stop_agent({})

    assert queue.get(task.task_id).state == TaskState.RUNNING


def test_bridge_returns_read_only_desktop_snapshot(tmp_path: Path) -> None:
    backend, _queue, store = build_backend(tmp_path)
    actions = build_default_action_registry()
    bridge = UIActionBridge(actions, Keymap(store, actions), state_provider=backend.snapshot)
    response = bridge.get_state()
    assert response["ok"] is True
    assert response["state"]["agents"][0]["agent_id"] == "nika.default"
