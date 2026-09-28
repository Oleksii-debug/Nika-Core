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
    RuntimeResumeProbe,
    RuntimeResumeProbeStatus,
    RuntimeResumeRequest,
)
from nika_core.ui.desktop_backend import DesktopBackend


class DurableBlockingRuntime:
    runtime_id = "desktop-active-pause-test"
    capabilities = frozenset(
        {RuntimeCapability.CANCELLATION, RuntimeCapability.DURABLE_RESUME}
    )

    def __init__(self) -> None:
        self.started = threading.Event()
        self.cancel_entered = threading.Event()
        self.release_cancel = threading.Event()
        self.cancelled = threading.Event()
        self.allow_run_exit = threading.Event()
        self.run_exited = threading.Event()
        self.resumed = threading.Event()

    def initial_resume_token(self, *, task_id: str, thread_id: str) -> str:
        return f"initial:{task_id}:{thread_id}"

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        self.started.set()
        while not self.cancelled.is_set():
            await asyncio.sleep(0.01)
        while not self.allow_run_exit.is_set():
            await asyncio.sleep(0.01)
        self.run_exited.set()
        return RuntimeResult(outcome=RuntimeOutcome.CANCELLED)

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        self.resumed.set()
        return RuntimeResult(outcome=RuntimeOutcome.COMPLETED)

    async def probe_resume(
        self,
        *,
        task_id: str,
        thread_id: str,
        resume_token: str,
    ) -> RuntimeResumeProbe:
        return RuntimeResumeProbe(
            status=RuntimeResumeProbeStatus.READY,
            reason="test checkpoint available",
            checkpoint_id=f"checkpoint:{task_id}:{thread_id}:{resume_token}",
        )

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        del task_id, thread_id
        self.cancel_entered.set()
        while not self.release_cancel.is_set():
            await asyncio.sleep(0.01)
        self.cancelled.set()
        return True


def _build_backend(
    tmp_path: Path,
    runtime: DurableBlockingRuntime,
) -> tuple[DesktopBackend, TaskQueue, AuditLog]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    queue = TaskQueue(store)
    audit = AuditLog(store)
    backend = DesktopBackend(
        queue=queue,
        agents=AgentRegistry(store),
        workspaces=WorkspaceRegistry(store),
        audit=audit,
        runtime=runtime,
    )
    return backend, queue, audit


def _wait_for_state(
    queue: TaskQueue,
    task_id: str,
    state: TaskState,
    *,
    timeout: float = 2.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if queue.get(task_id).state is state:
            return
        time.sleep(0.01)
    assert queue.get(task_id).state is state


def test_running_pause_is_nonblocking_serialized_and_durably_resumable(
    tmp_path: Path,
) -> None:
    runtime = DurableBlockingRuntime()
    backend, queue, audit = _build_backend(tmp_path, runtime)

    created = backend.create_task({"command": "довге завдання"})
    assert created.status == "accepted"
    assert runtime.started.wait(timeout=1)
    task_id = queue.list_recent()[0].task_id
    _wait_for_state(queue, task_id, TaskState.RUNNING)

    started_at = time.monotonic()
    pause = backend.pause_task({})
    elapsed = time.monotonic() - started_at

    assert pause.status == "accepted"
    assert pause.focus_id == "tasks-heading"
    assert elapsed < 0.5
    assert runtime.cancel_entered.wait(timeout=1)
    assert queue.get(task_id).state is TaskState.RUNNING

    with pytest.raises(ValueError, match="вже виконується"):
        backend.pause_task({})
    with pytest.raises(ValueError, match="запит на призупинення"):
        backend.stop_agent({})

    runtime.release_cancel.set()
    _wait_for_state(queue, task_id, TaskState.PAUSED)

    with pytest.raises(ValueError, match="завершує безпечне призупинення"):
        backend.resume_task({})

    runtime.allow_run_exit.set()
    assert runtime.run_exited.wait(timeout=1)
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        with backend._active_lock:
            pending_pause = backend._pause_futures.get(task_id)
        if pending_pause is None:
            break
        time.sleep(0.01)
    with backend._active_lock:
        assert backend._pause_futures.get(task_id) is None

    pause_events = audit.list_for(entity_type="task", entity_id=task_id)
    assert any(item.event_type == "runtime.pause_confirmed" for item in pause_events)
    assert not any(
        item.event_type.startswith("desktop.runtime_pause_") for item in pause_events
    )

    resumed = backend.resume_task({})
    assert resumed.status == "accepted"
    _wait_for_state(queue, task_id, TaskState.COMPLETED)
    assert runtime.resumed.wait(timeout=1)
    backend.close()


def test_ready_pause_keeps_existing_pre_runtime_behavior(tmp_path: Path) -> None:
    runtime = DurableBlockingRuntime()
    backend, queue, _audit = _build_backend(tmp_path, runtime)
    record = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "ще не запущено"},
    )
    queue.transition(record.task_id, TaskState.READY)

    result = backend.pause_task({})

    assert result.status == "completed"
    assert queue.get(record.task_id).state is TaskState.PAUSED
    assert runtime.started.is_set() is False
    backend.close()


class NonResumableBlockingRuntime(DurableBlockingRuntime):
    runtime_id = "desktop-active-pause-no-resume"
    capabilities = frozenset({RuntimeCapability.CANCELLATION})


class UnprovenDurableRuntime:
    runtime_id = "desktop-active-pause-unproven-resume"
    capabilities = frozenset(
        {RuntimeCapability.CANCELLATION, RuntimeCapability.DURABLE_RESUME}
    )

    def __init__(self) -> None:
        self.started = threading.Event()
        self.cancel_entered = threading.Event()
        self.cancelled = threading.Event()

    def initial_resume_token(self, *, task_id: str, thread_id: str) -> str:
        return f"initial:{task_id}:{thread_id}"

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        del request
        self.started.set()
        while not self.cancelled.is_set():
            await asyncio.sleep(0.01)
        return RuntimeResult(outcome=RuntimeOutcome.CANCELLED)

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        del request
        return RuntimeResult(outcome=RuntimeOutcome.COMPLETED)

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        del task_id, thread_id
        self.cancel_entered.set()
        self.cancelled.set()
        return True


def test_running_pause_requires_checkpoint_probe_before_effect(
    tmp_path: Path,
) -> None:
    runtime = UnprovenDurableRuntime()
    backend, queue, _audit = _build_backend(tmp_path, runtime)  # type: ignore[arg-type]

    backend.create_task({"command": "durable resume без checkpoint proof"})
    assert runtime.started.wait(timeout=1)
    task_id = queue.list_recent()[0].task_id
    _wait_for_state(queue, task_id, TaskState.RUNNING)

    with pytest.raises(TypeError, match="checkpoint"):
        backend.pause_task({})

    assert runtime.cancel_entered.is_set() is False
    assert queue.get(task_id).state is TaskState.RUNNING

    stopped = backend.stop_agent({})
    assert stopped.status == "accepted"
    assert runtime.cancel_entered.wait(timeout=1)
    _wait_for_state(queue, task_id, TaskState.CANCELLED)
    backend.close()


def test_running_pause_fails_before_effect_without_durable_resume_capability(
    tmp_path: Path,
) -> None:
    runtime = NonResumableBlockingRuntime()
    backend, queue, _audit = _build_backend(tmp_path, runtime)

    backend.create_task({"command": "без durable resume"})
    assert runtime.started.wait(timeout=1)
    task_id = queue.list_recent()[0].task_id
    _wait_for_state(queue, task_id, TaskState.RUNNING)

    with pytest.raises(ValueError, match="durable resume"):
        backend.pause_task({})

    assert runtime.cancel_entered.is_set() is False
    assert queue.get(task_id).state is TaskState.RUNNING

    stopped = backend.stop_agent({})
    assert stopped.status == "accepted"
    runtime.release_cancel.set()
    _wait_for_state(queue, task_id, TaskState.CANCELLED)
    runtime.allow_run_exit.set()
    assert runtime.run_exited.wait(timeout=1)
    backend.close()
