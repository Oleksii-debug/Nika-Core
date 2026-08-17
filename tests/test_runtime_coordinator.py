from __future__ import annotations

import asyncio

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.runtime.contracts import (
    RuntimeCapability,
    RuntimeEvent,
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResult,
    RuntimeResumeMode,
    RuntimeResumeRequest,
)
from nika_core.runtime.coordinator import TaskRuntimeCoordinator


class _ApprovalRuntime:
    runtime_id = "approval-proof"
    capabilities = frozenset(
        {RuntimeCapability.DURABLE_RESUME, RuntimeCapability.HUMAN_APPROVAL}
    )

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        return RuntimeResult(
            outcome=RuntimeOutcome.WAITING_APPROVAL,
            resume_token=request.thread_id,
            events=(
                RuntimeEvent(
                    0,
                    "runtime.approval_requested",
                    {"question": "approve?"},
                ),
            ),
        )

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        return RuntimeResult(
            outcome=RuntimeOutcome.COMPLETED,
            output={"approved": bool(request.value)},
        )

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        return False


class _ExplodingRuntime(_ApprovalRuntime):
    runtime_id = "exploding"

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        raise RuntimeError("boom")


def _ready_task(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    queue = TaskQueue(store)
    task = queue.create(workspace_id="test", agent_id="agent")
    queue.transition(task.task_id, TaskState.READY)
    return store, queue, task.task_id


def _task_state(store: SQLiteStore, task_id: str) -> TaskState:
    with store.connection() as conn:
        row = conn.execute("SELECT state FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    return TaskState(row["state"])


def test_coordinator_maps_approval_and_resume_into_task_state_and_audit(tmp_path) -> None:
    store, queue, task_id = _ready_task(tmp_path)
    audit = AuditLog(store)
    coordinator = TaskRuntimeCoordinator(queue, audit)
    runtime = _ApprovalRuntime()

    waiting = asyncio.run(
        coordinator.start(runtime, RuntimeRequest(task_id, "thread-1", {"goal": "safe"}))
    )
    assert waiting.outcome == RuntimeOutcome.WAITING_APPROVAL
    assert _task_state(store, task_id) == TaskState.WAITING_APPROVAL

    completed = asyncio.run(
        coordinator.resume_approval(
            runtime,
            RuntimeResumeRequest(
                task_id=task_id,
                thread_id="thread-1",
                resume_token="thread-1",
                mode=RuntimeResumeMode.APPROVAL,
                value=True,
            ),
        )
    )
    assert completed.outcome == RuntimeOutcome.COMPLETED
    assert _task_state(store, task_id) == TaskState.COMPLETED

    events = audit.list_for(entity_type="task", entity_id=task_id)
    types = [event.event_type for event in events]
    assert types == [
        "runtime.started",
        "runtime.approval_requested",
        "runtime.finished",
        "runtime.approval_resumed",
        "runtime.finished",
    ]
    assert events[2].payload["outcome"] == RuntimeOutcome.WAITING_APPROVAL.value
    assert events[-1].payload["outcome"] == RuntimeOutcome.COMPLETED.value


def test_coordinator_fails_task_closed_when_runtime_raises(tmp_path) -> None:
    store, queue, task_id = _ready_task(tmp_path)
    audit = AuditLog(store)
    coordinator = TaskRuntimeCoordinator(queue, audit)

    result = asyncio.run(
        coordinator.start(_ExplodingRuntime(), RuntimeRequest(task_id, "thread-error"))
    )
    assert result.outcome == RuntimeOutcome.FAILED
    assert result.error == "boom"
    assert _task_state(store, task_id) == TaskState.FAILED
    events = audit.list_for(entity_type="task", entity_id=task_id)
    assert events[-1].payload["outcome"] == RuntimeOutcome.FAILED.value


def test_coordinator_rejects_non_approval_resume_without_state_change(tmp_path) -> None:
    store, queue, task_id = _ready_task(tmp_path)
    queue.transition(task_id, TaskState.RUNNING)
    queue.transition(task_id, TaskState.WAITING_APPROVAL)
    coordinator = TaskRuntimeCoordinator(queue, AuditLog(store))

    request = RuntimeResumeRequest(
        task_id=task_id,
        thread_id="thread-2",
        resume_token="thread-2",
        mode=RuntimeResumeMode.CONTINUE,
    )
    try:
        asyncio.run(coordinator.resume_approval(_ApprovalRuntime(), request))
    except ValueError as exc:
        assert "APPROVAL" in str(exc)
    else:
        raise AssertionError("Expected ValueError")
    assert _task_state(store, task_id) == TaskState.WAITING_APPROVAL
