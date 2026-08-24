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

    assert result.status == "completed"
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
    assert backend.stop_agent({}).status == "completed"
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

    assert backend.stop_agent({}).status == "completed"
    assert runtime.cancelled.is_set()
    wait_for_state(queue, task.task_id, TaskState.CANCELLED)
    backend.close()


def test_pause_resume_before_runtime_start_uses_durable_task_history(tmp_path: Path) -> None:
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
    wait_for_state(queue, task.task_id, TaskState.COMPLETED)
    backend.close()


def test_resume_fails_closed_after_started_pause_without_runtime_session(tmp_path: Path) -> None:
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

    assert queue.get(task.task_id).state == TaskState.PAUSED


def test_stop_fails_closed_for_running_task_without_runtime_identity(tmp_path: Path) -> None:
    backend, queue, _store = build_backend(tmp_path)
    task = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "x"},
    )
    queue.transition(task.task_id, TaskState.READY)
    queue.transition(task.task_id, TaskState.RUNNING)

    with pytest.raises(ValueError, match="без збереженої або локально активної"):
        backend.stop_agent({})

    assert queue.get(task.task_id).state == TaskState.RUNNING


def test_bridge_returns_read_only_desktop_snapshot(tmp_path: Path) -> None:
    backend, _queue, store = build_backend(tmp_path)
    actions = build_default_action_registry()
    bridge = UIActionBridge(actions, Keymap(store, actions), state_provider=backend.snapshot)
    response = bridge.get_state()
    assert response["ok"] is True
    assert response["state"]["agents"][0]["agent_id"] == "nika.default"
