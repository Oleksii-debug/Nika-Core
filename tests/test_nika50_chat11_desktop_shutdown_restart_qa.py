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
from nika_core.runtime.session_store import RuntimeSessionStore
from nika_core.ui.desktop_backend import DesktopBackend


class _BlockingNonDurableRuntime:
    runtime_id = "qa-desktop-nondurable"
    capabilities = frozenset({RuntimeCapability.CANCELLATION})

    def __init__(self) -> None:
        self.started = threading.Event()
        self._release: asyncio.Event | None = None

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        self._release = asyncio.Event()
        self.started.set()
        await self._release.wait()
        return RuntimeResult(outcome=RuntimeOutcome.COMPLETED)

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        raise RuntimeUnsupportedError(
            f"non-durable QA runtime cannot resume task {request.task_id}"
        )

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        if self._release is None:
            return False
        self._release.set()
        return True


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


def test_graceful_desktop_close_cannot_leave_running_task_without_recovery_cursor(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    queue = TaskQueue(store)
    runtime = _BlockingNonDurableRuntime()
    backend = DesktopBackend(
        queue=queue,
        agents=AgentRegistry(store),
        workspaces=WorkspaceRegistry(store),
        audit=AuditLog(store),
        runtime=runtime,
    )

    accepted = backend.create_task({"command": "long local operation"})
    assert accepted.status == "accepted"
    assert runtime.started.wait(timeout=1.0)
    task = queue.list_recent()[0]
    _wait_for_state(queue, task.task_id, TaskState.RUNNING)

    # Exact precondition: this runtime intentionally exposes no durable resume cursor.
    assert RuntimeSessionStore(store).get(task.task_id) is None

    backend.close()

    durable_task = queue.get(task.task_id)
    durable_session = RuntimeSessionStore(store).get(task.task_id)
    assert not (
        durable_task.state is TaskState.RUNNING and durable_session is None
    ), (
        "graceful packaged-host teardown left a RUNNING task with no durable runtime "
        "session; after restart RuntimeRecoveryService cannot inventory or resume it"
    )
