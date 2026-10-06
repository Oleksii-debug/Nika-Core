from __future__ import annotations

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
    RuntimeCapability,
    RuntimeEvent,
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResult,
    RuntimeResumeRequest,
    RuntimeUnsupportedError,
)
from nika_core.ui.bridge import UIActionBridge
from nika_core.ui.desktop_backend import DesktopBackend


class BlockingRuntime:
    runtime_id = "desktop-blocking-test"
    capabilities = frozenset({RuntimeCapability.CANCELLATION})

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.cancelled = threading.Event()

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        self.started.set()
        while not self.release.is_set() and not self.cancelled.is_set():
            await asyncio.sleep(0.01)
        if self.cancelled.is_set():
            return RuntimeResult(outcome=RuntimeOutcome.CANCELLED)
        return RuntimeResult(
            outcome=RuntimeOutcome.COMPLETED,
            events=(RuntimeEvent(0, "blocking.completed", {"task_id": request.task_id}),),
        )

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        raise RuntimeUnsupportedError(f"resume is not supported for {request.task_id}")

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        del task_id, thread_id
        if not self.started.is_set() or self.cancelled.is_set():
            return False
        self.cancelled.set()
        return True


class MultiTaskBlockingRuntime:
    runtime_id = "desktop-multi-task-test"
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
                cancelled = request.task_id in self._cancelled
            if cancelled:
                return RuntimeResult(outcome=RuntimeOutcome.CANCELLED)
            if self.release.is_set():
                return RuntimeResult(outcome=RuntimeOutcome.COMPLETED)
            await asyncio.sleep(0.01)

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        raise RuntimeUnsupportedError(f"resume is not supported for {request.task_id}")

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        del thread_id
        with self._lock:
            if task_id not in self._started or task_id in self._cancelled:
                return False
            self._cancelled.add(task_id)
        return True

    def wait_started(self, count: int, *, timeout: float = 2) -> bool:
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


class PausingDurableRuntime:
    runtime_id = "desktop-pausing-durable-test"
    capabilities = frozenset(
        {RuntimeCapability.CANCELLATION, RuntimeCapability.DURABLE_RESUME}
    )

    def __init__(self) -> None:
        self.cancelled = threading.Event()

    def initial_resume_token(self, *, task_id: str, thread_id: str) -> str:
        return f"initial:{task_id}:{thread_id}"

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        return RuntimeResult(
            outcome=RuntimeOutcome.PAUSED,
            resume_token=f"paused:{request.task_id}:{request.thread_id}",
        )

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        return RuntimeResult(outcome=RuntimeOutcome.COMPLETED)

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        del task_id, thread_id
        self.cancelled.set()
        return True


def build_backend(
    tmp_path: Path,
    *,
    runtime=None,
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


def wait_for_state(
    queue: TaskQueue,
    task_id: str,
    state: TaskState,
    *,
    timeout: float = 2,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if queue.get(task_id).state == state:
            return
        time.sleep(0.01)
    assert queue.get(task_id).state == state


def create_ready_task(queue: TaskQueue, command: str) -> str:
    record = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": command},
    )
    queue.transition(record.task_id, TaskState.READY)
    return record.task_id


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
    assert result.status == "accepted"
    tasks = queue.list_recent()
    assert len(tasks) == 1
    assert tasks[0].payload["command"] == "перевір локальний стан"
    wait_for_state(queue, tasks[0].task_id, TaskState.COMPLETED)
    snapshot = backend.snapshot()
    assert snapshot["tasks"][0]["state"] == "COMPLETED"
    backend.close()


def test_create_task_returns_while_runtime_is_still_running(tmp_path: Path) -> None:
    runtime = BlockingRuntime()
    backend, queue, _store = build_backend(tmp_path, runtime=runtime)

    started_at = time.monotonic()
    result = backend.create_task({"command": "довге локальне завдання"})
    elapsed = time.monotonic() - started_at

    assert result.status == "accepted"
    assert elapsed < 0.5
    assert runtime.started.wait(timeout=1)
    task = queue.list_recent()[0]
    wait_for_state(queue, task.task_id, TaskState.RUNNING)
    assert runtime.release.is_set() is False

    runtime.release.set()
    wait_for_state(queue, task.task_id, TaskState.COMPLETED)
    backend.close()


def test_pause_running_runtime_fails_closed_instead_of_faking_pause(tmp_path: Path) -> None:
    runtime = BlockingRuntime()
    backend, queue, _store = build_backend(tmp_path, runtime=runtime)
    backend.create_task({"command": "довге завдання"})
    assert runtime.started.wait(timeout=1)
    task = queue.list_recent()[0]
    wait_for_state(queue, task.task_id, TaskState.RUNNING)

    with pytest.raises(ValueError, match="не підтримує безпечне активне призупинення"):
        backend.pause_task({})

    assert queue.get(task.task_id).state == TaskState.RUNNING
    assert backend.stop_agent({}).status == "accepted"
    assert runtime.cancelled.wait(timeout=1)
    wait_for_state(queue, task.task_id, TaskState.CANCELLED)
    backend.close()


def test_stop_cancels_live_non_durable_runtime_through_coordinator(tmp_path: Path) -> None:
    runtime = BlockingRuntime()
    backend, queue, _store = build_backend(tmp_path, runtime=runtime)
    backend.create_task({"command": "скасуй мене"})
    assert runtime.started.wait(timeout=1)
    task = queue.list_recent()[0]
    wait_for_state(queue, task.task_id, TaskState.RUNNING)
    assert backend._coordinator.sessions.get(task.task_id) is None

    assert backend.stop_agent({}).status == "accepted"
    assert runtime.cancelled.wait(timeout=1)
    wait_for_state(queue, task.task_id, TaskState.CANCELLED)
    backend.close()


def test_stop_uses_persisted_runtime_session_before_local_paused_state(
    tmp_path: Path,
) -> None:
    runtime = PausingDurableRuntime()
    backend, queue, _store = build_backend(tmp_path, runtime=runtime)
    backend.create_task({"command": "pause from runtime"})
    task = queue.list_recent()[0]
    wait_for_state(queue, task.task_id, TaskState.PAUSED)
    assert backend._coordinator.sessions.get(task.task_id) is not None

    assert backend.stop_agent({}).status == "accepted"
    assert runtime.cancelled.wait(timeout=1)
    wait_for_state(queue, task.task_id, TaskState.CANCELLED)
    assert backend._coordinator.sessions.get(task.task_id) is None
    backend.close()


def test_pause_resume_before_runtime_start_uses_durable_task_history(tmp_path: Path) -> None:
    backend, queue, _store = build_backend(tmp_path)
    task_id = create_ready_task(queue, "x")

    assert backend.pause_task({}).status == "completed"
    assert queue.get(task_id).state == TaskState.PAUSED
    assert backend.resume_task({}).status == "accepted"
    wait_for_state(queue, task_id, TaskState.COMPLETED)
    backend.close()


def test_resume_fails_closed_after_started_pause_without_runtime_session(tmp_path: Path) -> None:
    backend, queue, _store = build_backend(tmp_path)
    task_id = create_ready_task(queue, "x")
    queue.transition(task_id, TaskState.RUNNING)
    queue.transition(task_id, TaskState.PAUSED)

    with pytest.raises(ValueError, match="не має збереженої runtime-сесії"):
        backend.resume_task({})

    assert queue.get(task_id).state == TaskState.PAUSED


def test_stop_fails_closed_for_running_task_without_runtime_identity(tmp_path: Path) -> None:
    backend, queue, _store = build_backend(tmp_path)
    task_id = create_ready_task(queue, "x")
    queue.transition(task_id, TaskState.RUNNING)

    with pytest.raises(ValueError, match="без збереженої або локально активної"):
        backend.stop_agent({})

    assert queue.get(task_id).state == TaskState.RUNNING


def test_pause_fails_closed_when_unqualified_target_is_ambiguous(tmp_path: Path) -> None:
    backend, queue, _store = build_backend(tmp_path)
    first_id = create_ready_task(queue, "first ready task")
    second_id = create_ready_task(queue, "second ready task")

    with pytest.raises(ValueError, match="кілька завдань, доступних для призупинення"):
        backend.pause_task({})

    assert queue.get(first_id).state == TaskState.READY
    assert queue.get(second_id).state == TaskState.READY


def test_resume_fails_closed_when_unqualified_target_is_ambiguous(tmp_path: Path) -> None:
    backend, queue, _store = build_backend(tmp_path)
    first_id = create_ready_task(queue, "first paused task")
    second_id = create_ready_task(queue, "second paused task")
    queue.transition(first_id, TaskState.PAUSED)
    queue.transition(second_id, TaskState.PAUSED)

    with pytest.raises(ValueError, match="кілька завдань, доступних для продовження"):
        backend.resume_task({})

    assert queue.get(first_id).state == TaskState.PAUSED
    assert queue.get(second_id).state == TaskState.PAUSED


def test_stop_fails_closed_when_two_runtime_tasks_are_live(tmp_path: Path) -> None:
    runtime = MultiTaskBlockingRuntime()
    backend, queue, _store = build_backend(tmp_path, runtime=runtime)

    backend.create_task({"command": "first live task"})
    backend.create_task({"command": "second live task"})
    assert runtime.wait_started(2)

    records = {
        str(record.payload["command"]): record for record in queue.list_recent(limit=10)
    }
    first = records["first live task"]
    second = records["second live task"]
    wait_for_state(queue, first.task_id, TaskState.RUNNING)
    wait_for_state(queue, second.task_id, TaskState.RUNNING)

    try:
        with pytest.raises(ValueError, match="кілька завдань, доступних для зупинки"):
            backend.stop_agent({})

        assert runtime.cancelled_ids() == frozenset()
        assert queue.get(first.task_id).state == TaskState.RUNNING
        assert queue.get(second.task_id).state == TaskState.RUNNING
    finally:
        runtime.release.set()
        wait_for_state(queue, first.task_id, TaskState.COMPLETED)
        wait_for_state(queue, second.task_id, TaskState.COMPLETED)
        backend.close()


def test_bridge_returns_read_only_desktop_snapshot(tmp_path: Path) -> None:
    backend, _queue, store = build_backend(tmp_path)
    actions = build_default_action_registry()
    bridge = UIActionBridge(actions, Keymap(store, actions), state_provider=backend.snapshot)
    response = bridge.get_state()
    assert response["ok"] is True
    assert response["state"]["agents"][0]["agent_id"] == "nika.default"
