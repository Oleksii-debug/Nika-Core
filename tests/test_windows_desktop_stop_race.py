from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.agent_registry import AgentRegistry
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue, TaskRecord
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


class StopRaceRuntime:
    runtime_id = "desktop-stop-race-test"
    capabilities = frozenset({RuntimeCapability.CANCELLATION})

    def __init__(self) -> None:
        self.started = threading.Event()
        self.cancelled = threading.Event()
        self.release = threading.Event()

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        del request
        self.started.set()
        while not self.cancelled.is_set() and not self.release.is_set():
            await asyncio.sleep(0.01)
        if self.cancelled.is_set():
            return RuntimeResult(outcome=RuntimeOutcome.CANCELLED)
        return RuntimeResult(outcome=RuntimeOutcome.COMPLETED)

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        raise RuntimeUnsupportedError(f"resume is not supported for {request.task_id}")

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        del task_id, thread_id
        if not self.started.is_set() or self.cancelled.is_set():
            return False
        self.cancelled.set()
        return True


def _build_backend(
    tmp_path: Path,
    runtime: StopRaceRuntime,
) -> tuple[DesktopBackend, TaskQueue]:
    store = SQLiteStore(tmp_path / "desktop-stop-race.db")
    store.initialize()
    queue = TaskQueue(store)
    backend = DesktopBackend(
        queue=queue,
        agents=AgentRegistry(store),
        workspaces=WorkspaceRegistry(store),
        audit=AuditLog(store),
        runtime=runtime,
    )
    return backend, queue


def _wait_for_state(
    queue: TaskQueue,
    task_id: str,
    expected: TaskState,
    *,
    timeout: float = 2.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if queue.get(task_id).state == expected:
            return
        time.sleep(0.01)
    assert queue.get(task_id).state == expected


def _create_ready_task(queue: TaskQueue, command: str) -> TaskRecord:
    record = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": command},
    )
    queue.transition(record.task_id, TaskState.READY)
    return queue.get(record.task_id)


def test_stop_rechecks_runtime_authority_when_selected_record_is_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = StopRaceRuntime()
    backend, queue = _build_backend(tmp_path, runtime)
    stale_ready = _create_ready_task(queue, "race stale READY against live RUNNING")

    backend._schedule_start(stale_ready.task_id, str(stale_ready.payload["command"]))
    assert runtime.started.wait(timeout=1.0)
    _wait_for_state(queue, stale_ready.task_id, TaskState.RUNNING)

    def stale_target(*, action: str) -> TaskRecord:
        assert action == "зупинки"
        return stale_ready

    monkeypatch.setattr(backend, "_only_controllable", stale_target)

    try:
        result = backend.stop_agent({})
        assert result.status == "accepted"
        assert runtime.cancelled.wait(timeout=1.0)
        _wait_for_state(queue, stale_ready.task_id, TaskState.CANCELLED)
    finally:
        runtime.release.set()
        backend.close()


def test_stop_immediately_after_create_uses_submitted_runtime_identity(
    tmp_path: Path,
) -> None:
    runtime = StopRaceRuntime()
    backend, queue = _build_backend(tmp_path, runtime)

    create_result = backend.create_task({"command": "stop immediately after accepted create"})
    task = queue.list_recent(limit=1)[0]
    stop_result = backend.stop_agent({})

    try:
        assert create_result.status == "accepted"
        assert stop_result.status == "accepted"
        assert runtime.cancelled.wait(timeout=1.0)
        _wait_for_state(queue, task.task_id, TaskState.CANCELLED)
    finally:
        runtime.release.set()
        backend.close()
