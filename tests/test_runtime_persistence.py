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
from nika_core.runtime.idempotency import (
    IdempotencyConflictError,
    IdempotencyLedger,
    IdempotencyStatus,
)
from nika_core.runtime.session_store import RuntimeSessionStore


class _RestartApprovalRuntime:
    runtime_id = "restart-approval"
    capabilities = frozenset(
        {RuntimeCapability.DURABLE_RESUME, RuntimeCapability.HUMAN_APPROVAL}
    )

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        return RuntimeResult(
            outcome=RuntimeOutcome.WAITING_APPROVAL,
            resume_token=request.thread_id,
        )

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        return RuntimeResult(
            outcome=RuntimeOutcome.COMPLETED,
            output={"approved": request.value is True},
        )

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        return False


class _PausedRuntime(_RestartApprovalRuntime):
    runtime_id = "paused-runtime"

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        return RuntimeResult(outcome=RuntimeOutcome.PAUSED, resume_token=request.thread_id)


class _WrongRuntime(_RestartApprovalRuntime):
    runtime_id = "wrong-runtime"


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


def test_saved_approval_requires_explicit_authorization_api(tmp_path) -> None:
    store, queue, task_id = _ready(tmp_path)
    runtime = _RestartApprovalRuntime()
    first = TaskRuntimeCoordinator(queue, AuditLog(store))

    waiting = asyncio.run(first.start(runtime, RuntimeRequest(task_id, "thread-persist")))
    assert waiting.outcome == RuntimeOutcome.WAITING_APPROVAL
    assert _state(store, task_id) == TaskState.WAITING_APPROVAL

    persisted = RuntimeSessionStore(store).get(task_id)
    assert persisted is not None
    assert persisted.thread_id == "thread-persist"
    assert persisted.runtime_id == runtime.runtime_id

    recreated = TaskRuntimeCoordinator(TaskQueue(store), AuditLog(store))
    with pytest.raises(ValueError, match="explicit resume_saved_approval"):
        asyncio.run(recreated.resume_saved(runtime, task_id=task_id, value=True))

    assert _state(store, task_id) == TaskState.WAITING_APPROVAL
    assert recreated.sessions.get(task_id) is not None

    completed = asyncio.run(
        recreated.resume_saved_approval(runtime, task_id=task_id, approval_value=True)
    )
    assert completed.outcome == RuntimeOutcome.COMPLETED
    assert completed.output == {"approved": True}
    assert _state(store, task_id) == TaskState.COMPLETED
    assert recreated.sessions.get(task_id) is None

    event_types = [
        event.event_type
        for event in AuditLog(store).list_for(entity_type="task", entity_id=task_id)
    ]
    assert "runtime.saved_approval_resumed" in event_types


def test_saved_approval_api_rejects_non_approval_session(tmp_path) -> None:
    store, queue, task_id = _ready(tmp_path)
    runtime = _PausedRuntime()
    first = TaskRuntimeCoordinator(queue, AuditLog(store))
    asyncio.run(first.start(runtime, RuntimeRequest(task_id, "thread-paused-approval-check")))

    recreated = TaskRuntimeCoordinator(TaskQueue(store), AuditLog(store))
    with pytest.raises(ValueError, match="not waiting for human approval"):
        asyncio.run(
            recreated.resume_saved_approval(runtime, task_id=task_id, approval_value=True)
        )
    assert _state(store, task_id) == TaskState.PAUSED
    assert recreated.sessions.get(task_id) is not None


def test_paused_runtime_can_resume_after_process_recreation(tmp_path) -> None:
    store, queue, task_id = _ready(tmp_path)
    runtime = _PausedRuntime()
    first = TaskRuntimeCoordinator(queue, AuditLog(store))

    paused = asyncio.run(first.start(runtime, RuntimeRequest(task_id, "thread-paused")))
    assert paused.outcome == RuntimeOutcome.PAUSED
    assert _state(store, task_id) == TaskState.PAUSED

    recreated = TaskRuntimeCoordinator(TaskQueue(store), AuditLog(store))
    completed = asyncio.run(recreated.resume_saved(runtime, task_id=task_id))
    assert completed.outcome == RuntimeOutcome.COMPLETED
    assert _state(store, task_id) == TaskState.COMPLETED


def test_saved_resume_rejects_runtime_mismatch_without_consuming_session(tmp_path) -> None:
    store, queue, task_id = _ready(tmp_path)
    first = TaskRuntimeCoordinator(queue, AuditLog(store))
    asyncio.run(first.start(_RestartApprovalRuntime(), RuntimeRequest(task_id, "thread-owner")))

    recreated = TaskRuntimeCoordinator(TaskQueue(store), AuditLog(store))
    with pytest.raises(ValueError, match="belongs to runtime"):
        asyncio.run(recreated.resume_saved(_WrongRuntime(), task_id=task_id, value=True))
    assert _state(store, task_id) == TaskState.WAITING_APPROVAL
    assert recreated.sessions.get(task_id) is not None


def test_idempotency_ledger_deduplicates_and_rejects_key_reuse(tmp_path) -> None:
    store, _, task_id = _ready(tmp_path)
    ledger = IdempotencyLedger(store)

    first = ledger.reserve(
        operation_key="send:grant:42",
        task_id=task_id,
        operation_type="email.send",
        input_fingerprint="sha256:abc",
    )
    assert first.status == IdempotencyStatus.PENDING

    same = ledger.reserve(
        operation_key="send:grant:42",
        task_id=task_id,
        operation_type="email.send",
        input_fingerprint="sha256:abc",
    )
    assert same.operation_key == first.operation_key

    with pytest.raises(IdempotencyConflictError):
        ledger.reserve(
            operation_key="send:grant:42",
            task_id=task_id,
            operation_type="email.send",
            input_fingerprint="sha256:different",
        )

    completed = ledger.complete("send:grant:42", {"message_id": "m-1"})
    assert completed.status == IdempotencyStatus.COMPLETED
    assert completed.result == {"message_id": "m-1"}


def test_uncertain_side_effect_fails_closed_until_external_reconciliation(tmp_path) -> None:
    store, _, task_id = _ready(tmp_path)
    ledger = IdempotencyLedger(store)
    ledger.reserve(
        operation_key="publish:video:7",
        task_id=task_id,
        operation_type="publish",
        input_fingerprint="sha256:video",
    )
    uncertain = ledger.mark_uncertain("publish:video:7")
    assert uncertain.status == IdempotencyStatus.UNCERTAIN

    with pytest.raises(IdempotencyConflictError, match="reconciliation"):
        ledger.complete("publish:video:7", {"published": True})

    reconciled = ledger.reconcile_completed(
        "publish:video:7",
        {"remote_id": "video-7"},
    )
    assert reconciled.status == IdempotencyStatus.COMPLETED
    assert reconciled.result == {"remote_id": "video-7"}
