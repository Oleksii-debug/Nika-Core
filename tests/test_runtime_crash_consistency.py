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
from nika_core.runtime.session_store import RuntimeSessionStore


class _DurableRuntime:
    runtime_id = "atomic-proof"
    capabilities = frozenset({RuntimeCapability.DURABLE_RESUME})

    def __init__(self, result: RuntimeResult | None = None) -> None:
        self.run_calls = 0
        self.result = result or RuntimeResult(
            outcome=RuntimeOutcome.COMPLETED,
            output={"ok": True},
        )

    @staticmethod
    def initial_resume_token(*, task_id: str, thread_id: str) -> str:
        del task_id
        return thread_id

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        self.run_calls += 1
        return self.result

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
    def delete_with_connection(self, conn: sqlite3.Connection, task_id: str) -> None:
        super().delete_with_connection(conn, task_id)
        raise RuntimeError("injected session-delete failure")


class _FailFinishedAudit(AuditLog):
    def append_with_connection(
        self,
        conn: sqlite3.Connection,
        *,
        event_type: str,
        entity_type: str,
        entity_id: str,
        payload: dict[str, object] | None = None,
    ) -> int:
        event_id = super().append_with_connection(
            conn,
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            payload=payload,
        )
        if event_type == "runtime.finished":
            raise RuntimeError("injected final audit failure")
        return event_id


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
    runtime = _DurableRuntime()
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
    runtime = _DurableRuntime()
    coordinator = TaskRuntimeCoordinator(queue, AuditLog(store), session_store=sessions)

    with pytest.raises(ValueError, match="already owns a persisted runtime session"):
        asyncio.run(coordinator.start(runtime, RuntimeRequest(task_id, "thread-replacement")))

    assert _state(store, task_id) == TaskState.READY
    persisted = sessions.get(task_id)
    assert persisted is not None
    assert persisted.thread_id == "thread-original"
    assert persisted.resume_token == "thread-original"
    assert runtime.run_calls == 0


def test_terminal_session_delete_failure_rolls_back_entire_local_finalization(tmp_path) -> None:
    store, queue, task_id = _ready_task(tmp_path)
    runtime = _DurableRuntime()
    sessions = _FailSessionDelete(store)
    coordinator = TaskRuntimeCoordinator(queue, AuditLog(store), session_store=sessions)

    with pytest.raises(RuntimeError, match="session-delete failure"):
        asyncio.run(coordinator.start(runtime, RuntimeRequest(task_id, "thread-finish-window")))

    assert runtime.run_calls == 1
    assert _state(store, task_id) == TaskState.RUNNING
    persisted = RuntimeSessionStore(store).get(task_id)
    assert persisted is not None
    assert persisted.is_active
    assert persisted.thread_id == "thread-finish-window"


def test_terminal_audit_failure_rolls_back_state_and_session_delete(tmp_path) -> None:
    store, queue, task_id = _ready_task(tmp_path)
    runtime = _DurableRuntime()
    coordinator = TaskRuntimeCoordinator(queue, _FailFinishedAudit(store))

    with pytest.raises(RuntimeError, match="final audit failure"):
        asyncio.run(coordinator.start(runtime, RuntimeRequest(task_id, "thread-audit-terminal")))

    assert _state(store, task_id) == TaskState.RUNNING
    persisted = RuntimeSessionStore(store).get(task_id)
    assert persisted is not None
    assert persisted.is_active
    event_types = [
        event.event_type for event in AuditLog(store).list_for(entity_type="task", entity_id=task_id)
    ]
    assert "runtime.finished" not in event_types


def test_resumable_audit_failure_rolls_back_wait_state_and_new_resume_cursor(tmp_path) -> None:
    store, queue, task_id = _ready_task(tmp_path)
    runtime = _DurableRuntime(
        RuntimeResult(
            outcome=RuntimeOutcome.WAITING_APPROVAL,
            resume_token="checkpoint-after-prompt",
        )
    )
    coordinator = TaskRuntimeCoordinator(queue, _FailFinishedAudit(store))

    with pytest.raises(RuntimeError, match="final audit failure"):
        asyncio.run(coordinator.start(runtime, RuntimeRequest(task_id, "thread-audit-resumable")))

    assert _state(store, task_id) == TaskState.RUNNING
    persisted = RuntimeSessionStore(store).get(task_id)
    assert persisted is not None
    assert persisted.is_active
    assert persisted.resume_token == "thread-audit-resumable"
    event_types = [
        event.event_type for event in AuditLog(store).list_for(entity_type="task", entity_id=task_id)
    ]
    assert "runtime.finished" not in event_types
