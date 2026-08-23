from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.runtime.contracts import (
    RuntimeOutcome,
    RuntimeResult,
    RuntimeResumeProbe,
    RuntimeResumeProbeStatus,
)
from nika_core.runtime.coordinator import (
    RuntimeRecoveryClaimConflict,
    TaskRuntimeCoordinator,
)
from nika_core.runtime.idempotency import IdempotencyLedger, IdempotencyStatus
from nika_core.runtime.recovery import RecoveryDisposition, RuntimeRecoveryService
from nika_core.runtime.registry import RuntimeRegistry
from nika_core.runtime.session_store import RuntimeSessionStore


class _SimulatedProcessLoss(BaseException):
    pass


@dataclass
class _SharedResumeGate:
    calls: list[str] = field(default_factory=list)
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)


class _ClaimedRuntime:
    runtime_id = "claim-proof"
    capabilities = frozenset()

    def __init__(
        self,
        gate: _SharedResumeGate,
        owner: str,
        *,
        process_loss: bool = False,
    ) -> None:
        self._gate = gate
        self._owner = owner
        self._process_loss = process_loss

    async def run(self, request):
        raise AssertionError("recovery must resume, not run")

    async def resume(self, request):
        self._gate.calls.append(self._owner)
        self._gate.entered.set()
        if self._process_loss:
            raise _SimulatedProcessLoss()
        await self._gate.release.wait()
        return RuntimeResult(
            outcome=RuntimeOutcome.COMPLETED,
            output={"owner": self._owner, "task_id": request.task_id},
        )

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        del task_id, thread_id
        return False

    async def probe_resume(self, *, task_id: str, thread_id: str, resume_token: str):
        del task_id
        assert resume_token == thread_id
        return RuntimeResumeProbe(
            status=RuntimeResumeProbeStatus.READY,
            reason="durable checkpoint exists",
            checkpoint_id=f"checkpoint:{thread_id}",
        )


def _active_session(path: Path) -> tuple[SQLiteStore, TaskQueue, str]:
    store = SQLiteStore(path)
    store.initialize()
    queue = TaskQueue(store)
    task = queue.create(workspace_id="workspace", agent_id="agent")
    queue.transition(task.task_id, TaskState.READY)
    queue.transition(task.task_id, TaskState.RUNNING)
    RuntimeSessionStore(store).record_active(
        task_id=task.task_id,
        runtime_id="claim-proof",
        thread_id="shared-thread",
        resume_token="shared-thread",
    )
    return store, queue, task.task_id


def _recovery(
    path: Path,
    gate: _SharedResumeGate,
    owner: str,
    *,
    process_loss: bool = False,
) -> RuntimeRecoveryService:
    store = SQLiteStore(path)
    store.initialize()
    queue = TaskQueue(store)
    registry = RuntimeRegistry()
    registry.register(_ClaimedRuntime(gate, owner, process_loss=process_loss))
    coordinator = TaskRuntimeCoordinator(
        queue,
        AuditLog(store),
        recovery_owner_id=owner,
    )
    return RuntimeRecoveryService(
        queue=queue,
        audit=AuditLog(store),
        runtimes=registry,
        coordinator=coordinator,
        sessions=RuntimeSessionStore(store),
        idempotency=IdempotencyLedger(store),
    )


def test_two_startup_owners_have_one_durable_resume_winner(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "nika.db"
        store, _queue, task_id = _active_session(path)
        gate = _SharedResumeGate()
        first = _recovery(path, gate, "process-a")
        second = _recovery(path, gate, "process-b")

        first_task = asyncio.create_task(first.resume_safe_crash_sessions(max_count=1))
        await gate.entered.wait()
        second_result = await second.resume_safe_crash_sessions(max_count=1)

        assert gate.calls == ["process-a"]
        assert len(second_result) <= 1
        if second_result:
            assert second_result[0].result is None
            assert second_result[0].error

        pending = [
            record
            for record in IdempotencyLedger(store).list_for_task(task_id)
            if record.operation_type == "runtime.recovery_resume"
        ]
        assert len(pending) == 1
        assert pending[0].status == IdempotencyStatus.PENDING

        gate.release.set()
        first_result = await first_task
        assert len(first_result) == 1
        assert first_result[0].succeeded
        assert gate.calls == ["process-a"]
        assert pending[0].operation_key.startswith("runtime.recovery:")
        completed = IdempotencyLedger(store).require(pending[0].operation_key)
        assert completed.status == IdempotencyStatus.COMPLETED
        assert completed.result is not None
        assert completed.result["owner_id"] == "process-a"

    asyncio.run(scenario())


def test_direct_resume_saved_rejects_second_process_owner(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "explicit.db"
        store, queue, task_id = _active_session(path)
        gate = _SharedResumeGate()
        runtime_a = _ClaimedRuntime(gate, "process-a")
        runtime_b = _ClaimedRuntime(gate, "process-b")
        first = TaskRuntimeCoordinator(
            queue,
            AuditLog(store),
            recovery_owner_id="process-a",
        )
        second = TaskRuntimeCoordinator(
            TaskQueue(store),
            AuditLog(store),
            recovery_owner_id="process-b",
        )

        first_task = asyncio.create_task(first.resume_saved(runtime_a, task_id=task_id))
        await gate.entered.wait()
        with pytest.raises(RuntimeRecoveryClaimConflict):
            await second.resume_saved(runtime_b, task_id=task_id)
        assert gate.calls == ["process-a"]

        gate.release.set()
        result = await first_task
        assert result.outcome == RuntimeOutcome.COMPLETED
        assert gate.calls == ["process-a"]

    asyncio.run(scenario())


def test_process_loss_leaves_pending_claim_that_blocks_restart(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "loss.db"
        store, _queue, task_id = _active_session(path)
        gate = _SharedResumeGate()
        first = _recovery(path, gate, "lost-owner", process_loss=True)

        with pytest.raises(_SimulatedProcessLoss):
            await first.resume_safe_crash_sessions(max_count=1)

        claims = [
            record
            for record in IdempotencyLedger(store).list_for_task(task_id)
            if record.operation_type == "runtime.recovery_resume"
        ]
        assert len(claims) == 1
        assert claims[0].status == IdempotencyStatus.PENDING

        restarted = _recovery(path, _SharedResumeGate(), "replacement-owner")
        candidate = restarted.inspect()[0]
        assert candidate.disposition == RecoveryDisposition.RECONCILE_SIDE_EFFECTS
        assert candidate.unresolved_operation_keys == (claims[0].operation_key,)

    asyncio.run(scenario())
