from __future__ import annotations

import asyncio
import sqlite3

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
from nika_core.runtime.recovery import RecoveryDisposition, RuntimeRecoveryService
from nika_core.runtime.registry import RuntimeRegistry
from nika_core.runtime.session_store import RuntimeSessionStore


class _DurableCompletingRuntime:
    runtime_id = "atomic-proof"
    capabilities = frozenset({RuntimeCapability.DURABLE_RESUME})

    def __init__(self) -> None:
        self.run_calls = 0

    @staticmethod
    def initial_resume_token(*, task_id: str, thread_id: str) -> str:
        del task_id
        return thread_id

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        self.run_calls += 1
        return RuntimeResult(outcome=RuntimeOutcome.COMPLETED, output={"ok": True})

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        return RuntimeResult(outcome=RuntimeOutcome.COMPLETED, output={"resumed": True})

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        return False


class _FailAfterCursorInsert(RuntimeSessionStore):
    def record_active_with_connection(
        self,
        conn: sqlite3.Connection,
        *,
        task_id: str,
        runtime_id: str,
        thread_id: str,
        resume_token: str,
    ) -> None:
        super().record_active_with_connection(
            conn,
            task_id=task_id,
            runtime_id=runtime_id,
            thread_id=thread_id,
            resume_token=resume_token,
        )
        raise RuntimeError("injected durable-start failure")


class _FailSessionDelete(RuntimeSessionStore):
    def delete(self, task_id: str) -> None:
        raise RuntimeError("injected session-delete failure")


def _ready_task(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    queue = TaskQueue(store)
    task = queue.create(workspace_id="proof", agent_id="agent")
    queue.transition(task.task_id, TaskState.READY)
    return store, queue, task.task_id


def _state(store: SQLiteStore, task_id: str) -> TaskState:
    with store.connection() as conn:
        row = conn.execute("SELECT state FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    return TaskState(row["state"])


def test_injected_durable_start_failure_rolls_back_task_and_cursor(tmp_path) -> None:
    store, queue, task_id = _ready_task(tmp_path)
    runtime = _DurableCompletingRuntime()
    sessions = _FailAfterCursorInsert(store)
    coordinator = TaskRuntimeCoordinator(queue, AuditLog(store), session_store=sessions)

    with pytest.raises(RuntimeError, match="injected durable-start failure"):
        asyncio.run(coordinator.start(runtime, RuntimeRequest(task_id, "thread-atomic")))

    assert _state(store, task_id) == TaskState.READY
    assert RuntimeSessionStore(store).get(task_id) is None
    assert runtime.run_calls == 0


def test_fresh_start_cannot_overwrite_existing_recovery_cursor(tmp_path) -> None:
    store, queue, task_id = _ready_task(tmp_path)
    sessions = RuntimeSessionStore(store)
    sessions.record_active(
        task_id=task_id,
        runtime_id="atomic-proof",
        thread_id="thread-original",
        resume_token="thread-original",
    )
    runtime = _DurableCompletingRuntime()
    coordinator = TaskRuntimeCoordinator(queue, AuditLog(store), session_store=sessions)

    with pytest.raises(ValueError, match="already owns a persisted runtime session"):
        asyncio.run(coordinator.start(runtime, RuntimeRequest(task_id, "thread-replacement")))

    assert _state(store, task_id) == TaskState.READY
    persisted = sessions.get(task_id)
    assert persisted is not None
    assert persisted.thread_id == "thread-original"
    assert persisted.resume_token == "thread-original"
    assert runtime.run_calls == 0


def test_terminal_task_left_with_session_pointer_is_never_auto_reopened(tmp_path) -> None:
    store, queue, task_id = _ready_task(tmp_path)
    runtime = _DurableCompletingRuntime()
    sessions = _FailSessionDelete(store)
    coordinator = TaskRuntimeCoordinator(queue, AuditLog(store), session_store=sessions)

    with pytest.raises(RuntimeError, match="session-delete failure"):
        asyncio.run(coordinator.start(runtime, RuntimeRequest(task_id, "thread-finish-window")))

    assert runtime.run_calls == 1
    assert _state(store, task_id) == TaskState.COMPLETED
    persisted = RuntimeSessionStore(store).get(task_id)
    assert persisted is not None
    assert persisted.is_active

    registry = RuntimeRegistry()
    registry.register(runtime)
    recovery = RuntimeRecoveryService(
        queue=TaskQueue(store),
        audit=AuditLog(store),
        runtimes=registry,
        sessions=RuntimeSessionStore(store),
    )
    candidate = recovery.inspect()[0]
    assert candidate.task_id == task_id
    assert candidate.disposition == RecoveryDisposition.INCONSISTENT_STATE
    assert _state(store, task_id) == TaskState.COMPLETED
