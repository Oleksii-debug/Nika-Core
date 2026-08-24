from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.agent_registry import AgentRegistry
from nika_core.kernel.audit import AuditLog
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
from nika_core.ui.desktop_backend import DesktopBackend


class SlowCancelRuntime:
    runtime_id = "qa-desktop-slow-cancel"
    capabilities = frozenset({RuntimeCapability.CANCELLATION})

    def __init__(self) -> None:
        self.started = threading.Event()
        self.cancel_entered = threading.Event()
        self.release_cancel = threading.Event()
        self.release_run = threading.Event()
        self._cancel_event: asyncio.Event | None = None
        self._task_id: str | None = None
        self._thread_id: str | None = None

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        self._task_id = request.task_id
        self._thread_id = request.thread_id
        self._cancel_event = asyncio.Event()
        self.started.set()
        while not self.release_run.is_set() and not self._cancel_event.is_set():
            await asyncio.sleep(0.01)
        if self._cancel_event.is_set():
            return RuntimeResult(outcome=RuntimeOutcome.CANCELLED)
        return RuntimeResult(outcome=RuntimeOutcome.COMPLETED)

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        raise RuntimeUnsupportedError(f"resume is not supported for {request.task_id}")

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        if (
            task_id != self._task_id
            or thread_id != self._thread_id
            or self._cancel_event is None
        ):
            return False
        self.cancel_entered.set()
        while not self.release_cancel.is_set():
            await asyncio.sleep(0.01)
        self._cancel_event.set()
        return True


def _build_backend(
    tmp_path: Path,
    runtime: AgentRuntimePort,
) -> tuple[DesktopBackend, TaskQueue]:
    store = SQLiteStore(tmp_path / "desktop-stop-responsiveness.db")
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
        if queue.get(task_id).state is expected:
            return
        time.sleep(0.01)
    current = queue.get(task_id).state
    raise AssertionError(
        f"task {task_id} did not reach {expected.value}; current={current.value}"
    )


def test_create_reports_dispatch_as_accepted_while_runtime_is_live(tmp_path: Path) -> None:
    runtime = SlowCancelRuntime()
    backend, queue = _build_backend(tmp_path, runtime)

    result = backend.create_task({"command": "довге локальне завдання"})
    assert runtime.started.wait(timeout=1.0)
    task = queue.list_recent()[0]
    _wait_for_state(queue, task.task_id, TaskState.RUNNING)

    runtime.release_run.set()
    _wait_for_state(queue, task.task_id, TaskState.COMPLETED)
    backend.close()

    assert result.status == "accepted"


def test_stop_returns_before_runtime_cancel_finishes(tmp_path: Path) -> None:
    runtime = SlowCancelRuntime()
    backend, queue = _build_backend(tmp_path, runtime)
    backend.create_task({"command": "зупини мене без блокування bridge"})
    assert runtime.started.wait(timeout=1.0)
    task = queue.list_recent()[0]
    _wait_for_state(queue, task.task_id, TaskState.RUNNING)

    result_holder: dict[str, object] = {}
    stop_finished = threading.Event()

    def invoke_stop() -> None:
        try:
            result_holder["result"] = backend.stop_agent({})
        except Exception as exc:  # noqa: BLE001 - capture public bridge failure for assertion
            result_holder["error"] = exc
        finally:
            stop_finished.set()

    stop_thread = threading.Thread(target=invoke_stop, name="qa-desktop-stop")
    stop_thread.start()
    assert runtime.cancel_entered.wait(timeout=1.0)

    returned_while_cancel_pending = stop_finished.wait(timeout=0.2)
    runtime.release_cancel.set()
    stop_thread.join(timeout=2.0)
    assert not stop_thread.is_alive()
    _wait_for_state(queue, task.task_id, TaskState.CANCELLED)
    backend.close()

    assert returned_while_cancel_pending, (
        "DesktopBackend.stop_agent() blocked the synchronous WebView bridge "
        "until runtime cancellation completed"
    )
    assert "error" not in result_holder
    result = result_holder["result"]
    assert getattr(result, "status") == "accepted"
