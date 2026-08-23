from __future__ import annotations

import asyncio
from datetime import UTC, datetime as RealDateTime

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.runtime import session_store as session_store_module
from nika_core.runtime.contracts import (
    RuntimeCapability,
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResult,
    RuntimeResumeProbe,
    RuntimeResumeProbeStatus,
    RuntimeResumeRequest,
)
from nika_core.runtime.coordinator import TaskRuntimeCoordinator
from nika_core.runtime.idempotency import IdempotencyLedger, IdempotencyStatus


class _FrozenDateTime(RealDateTime):
    @classmethod
    def now(cls, tz=None):
        fixed = cls(2026, 8, 23, 20, 0, 0, tzinfo=UTC)
        return fixed if tz is not None else fixed.replace(tzinfo=None)


class _RepeatedApprovalRuntime:
    runtime_id = "repeated-approval-proof"
    capabilities = frozenset(
        {RuntimeCapability.DURABLE_RESUME, RuntimeCapability.HUMAN_APPROVAL}
    )

    def __init__(self) -> None:
        self.resume_calls = 0
        self.checkpoint_id = "checkpoint-0"

    @staticmethod
    def initial_resume_token(*, task_id: str, thread_id: str) -> str:
        del task_id
        return thread_id

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        return RuntimeResult(
            outcome=RuntimeOutcome.WAITING_APPROVAL,
            resume_token=request.thread_id,
        )

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        self.resume_calls += 1
        self.checkpoint_id = f"checkpoint-{self.resume_calls}"
        if self.resume_calls == 1:
            return RuntimeResult(
                outcome=RuntimeOutcome.WAITING_APPROVAL,
                resume_token=request.resume_token,
            )
        return RuntimeResult(outcome=RuntimeOutcome.COMPLETED, output={"approved": True})

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        del task_id, thread_id
        return False

    async def probe_resume(self, *, task_id: str, thread_id: str, resume_token: str):
        del task_id
        assert resume_token == thread_id
        return RuntimeResumeProbe(
            status=RuntimeResumeProbeStatus.READY,
            reason="approval checkpoint exists",
            checkpoint_id=self.checkpoint_id,
        )


def test_repeated_resumable_results_get_distinct_session_epochs_under_frozen_clock(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_store_module, "datetime", _FrozenDateTime)

    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    queue = TaskQueue(store)
    task = queue.create(workspace_id="epoch-proof", agent_id="agent")
    queue.transition(task.task_id, TaskState.READY)
    runtime = _RepeatedApprovalRuntime()
    coordinator = TaskRuntimeCoordinator(queue, AuditLog(store))

    waiting = asyncio.run(
        coordinator.start(runtime, RuntimeRequest(task.task_id, "repeated-thread"))
    )
    assert waiting.outcome == RuntimeOutcome.WAITING_APPROVAL
    first_epoch = coordinator.sessions.get(task.task_id)
    assert first_epoch is not None

    waiting_again = asyncio.run(
        coordinator.resume_saved_approval(
            runtime,
            task_id=task.task_id,
            approval_value=True,
        )
    )
    assert waiting_again.outcome == RuntimeOutcome.WAITING_APPROVAL
    second_epoch = coordinator.sessions.get(task.task_id)
    assert second_epoch is not None
    assert second_epoch.updated_at > first_epoch.updated_at

    completed = asyncio.run(
        coordinator.resume_saved_approval(
            runtime,
            task_id=task.task_id,
            approval_value=True,
        )
    )
    assert completed.outcome == RuntimeOutcome.COMPLETED
    assert runtime.resume_calls == 2
    assert queue.get(task.task_id).state == TaskState.COMPLETED

    claims = [
        record
        for record in IdempotencyLedger(store).list_for_task(task.task_id)
        if record.operation_type == "runtime.recovery_resume"
    ]
    assert len(claims) == 2
    assert claims[0].operation_key != claims[1].operation_key
    assert all(record.status == IdempotencyStatus.COMPLETED for record in claims)
