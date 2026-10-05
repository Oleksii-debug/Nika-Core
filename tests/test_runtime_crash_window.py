from __future__ import annotations

import asyncio

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.runtime.contracts import (
    RuntimeCapability,
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResult,
    RuntimeResumeRequest,
)
from nika_core.runtime.coordinator import TaskRuntimeCoordinator


class _SimulatedProcessLoss(BaseException):
    """Deliberately bypass normal Exception handling like abrupt process loss."""


class _CrashRecoverableRuntime:
    runtime_id = "crash-recoverable"
    capabilities = frozenset({RuntimeCapability.DURABLE_RESUME})

    @staticmethod
    def initial_resume_token(*, task_id: str, thread_id: str) -> str:
        del task_id
        return thread_id

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        raise _SimulatedProcessLoss()

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        return RuntimeResult(
            outcome=RuntimeOutcome.COMPLETED,
            output={"recovered_thread": request.thread_id},
        )

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        return False


class _CompletesNormally(_CrashRecoverableRuntime):
    runtime_id = "normal-durable"

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        return RuntimeResult(outcome=RuntimeOutcome.COMPLETED, output={"ok": True})


def _ready(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    queue = TaskQueue(store)
    task = queue.create(workspace_id="research", agent_id="agent")
    queue.transition(task.task_id, TaskState.READY)
    return store, queue, task.task_id


def _state(store: SQLiteStore, task_id: str) -> TaskState:
    with store.connection() as conn:
        row = conn.execute("SELECT state FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    return TaskState(row["state"])


def test_process_loss_before_runtime_result_keeps_durable_task_pointer(tmp_path) -> None:
    store, queue, task_id = _ready(tmp_path)
    runtime = _CrashRecoverableRuntime()
    first = TaskRuntimeCoordinator(queue, AuditLog(store))

    with pytest.raises(_SimulatedProcessLoss):
        asyncio.run(first.start(runtime, RuntimeRequest(task_id, "thread-before-result")))

    assert _state(store, task_id) == TaskState.RUNNING
    active = first.sessions.get(task_id)
    assert active is not None
    assert active.is_active
    assert active.runtime_id == runtime.runtime_id
    assert active.thread_id == "thread-before-result"
    assert active.resume_token == "thread-before-result"

    recreated = TaskRuntimeCoordinator(TaskQueue(store), AuditLog(store))
    completed = asyncio.run(recreated.resume_saved(runtime, task_id=task_id))

    assert completed.outcome == RuntimeOutcome.COMPLETED
    assert completed.output == {"recovered_thread": "thread-before-result"}
    assert _state(store, task_id) == TaskState.COMPLETED
    assert recreated.sessions.get(task_id) is None

    event_types = [
        event.event_type
        for event in AuditLog(store).list_for(entity_type="task", entity_id=task_id)
    ]
    assert "runtime.session_bound" in event_types
    assert "runtime.crash_recovery_started" in event_types
    assert "runtime.saved_resume_started" in event_types


def test_normal_terminal_result_removes_prebound_active_session(tmp_path) -> None:
    store, queue, task_id = _ready(tmp_path)
    coordinator = TaskRuntimeCoordinator(queue, AuditLog(store))

    result = asyncio.run(
        coordinator.start(_CompletesNormally(), RuntimeRequest(task_id, "thread-normal"))
    )

    assert result.outcome == RuntimeOutcome.COMPLETED
    assert _state(store, task_id) == TaskState.COMPLETED
    assert coordinator.sessions.get(task_id) is None


def test_stale_active_pointer_cannot_reopen_terminal_task(tmp_path) -> None:
    store, queue, task_id = _ready(tmp_path)
    runtime = _CrashRecoverableRuntime()
    coordinator = TaskRuntimeCoordinator(queue, AuditLog(store))
    coordinator.sessions.record_active(
        task_id=task_id,
        runtime_id=runtime.runtime_id,
        thread_id="thread-stale",
        resume_token="thread-stale",
    )
    queue.transition(task_id, TaskState.RUNNING)
    queue.transition(task_id, TaskState.COMPLETED)

    recreated = TaskRuntimeCoordinator(TaskQueue(store), AuditLog(store))
    with pytest.raises(ValueError, match="incompatible task state"):
        asyncio.run(recreated.resume_saved(runtime, task_id=task_id))

    assert _state(store, task_id) == TaskState.COMPLETED
    assert recreated.sessions.get(task_id) is not None
