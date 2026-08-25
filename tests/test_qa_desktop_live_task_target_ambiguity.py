from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.agent_registry import AgentRegistry
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.kernel.workspace_registry import WorkspaceRegistry
from nika_core.runtime.contracts import (
    RuntimeCapability,
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResult,
    RuntimeResumeRequest,
    RuntimeUnsupportedError,
)
from nika_core.ui.desktop_backend import DesktopBackend


class MultiTaskBlockingRuntime:
    runtime_id = "qa-desktop-multi-task"
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


def _build_backend(
    tmp_path: Path,
    runtime: MultiTaskBlockingRuntime,
) -> tuple[DesktopBackend, TaskQueue]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    queue = TaskQueue(store)
    return (
        DesktopBackend(
            queue=queue,
            agents=AgentRegistry(store),
            workspaces=WorkspaceRegistry(store),
            audit=AuditLog(store),
            runtime=runtime,
        ),
        queue,
    )


def _wait_for_state(
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


def test_stop_without_task_target_fails_closed_when_two_runtime_tasks_are_live(
    tmp_path: Path,
) -> None:
    runtime = MultiTaskBlockingRuntime()
    backend, queue = _build_backend(tmp_path, runtime)

    backend.create_task({"command": "first live task"})
    backend.create_task({"command": "second live task"})
    assert runtime.wait_started(2)

    records = {
        str(record.payload["command"]): record
        for record in queue.list_recent(limit=10)
    }
    first = records["first live task"]
    second = records["second live task"]
    _wait_for_state(queue, first.task_id, TaskState.RUNNING)
    _wait_for_state(queue, second.task_id, TaskState.RUNNING)

    try:
        with pytest.raises(ValueError):
            backend.stop_agent({})

        assert runtime.cancelled_ids() == frozenset()
        assert queue.get(first.task_id).state == TaskState.RUNNING
        assert queue.get(second.task_id).state == TaskState.RUNNING
    finally:
        runtime.release.set()
        for record in (first, second):
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if queue.get(record.task_id).state in {
                    TaskState.COMPLETED,
                    TaskState.CANCELLED,
                }:
                    break
                time.sleep(0.01)
        backend.close()
