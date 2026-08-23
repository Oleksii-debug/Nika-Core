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
    checkpoint_id: str = "checkpoint:initial"


class _ClaimedRuntime:
    runtime_id = "claim-proof"
    capabilities = frozenset()

    def __init__(
        self,
        gate: _SharedResumeGate,
        owner: str,
        *,
        process_loss: bool = False,
        advance_checkpoint: bool = False,
        block: bool = True,
    ) -> None:
        self._gate = gate
        self._owner = owner
        self._process_loss = process_loss
        self._advance_checkpoint = advance_checkpoint
        self._block = block

    async def run(self, request):
        raise AssertionError("recovery must resume, not run")

    async def resume(self, request):
        self._gate.calls.append(self._owner)
        if self._advance_checkpoint:
            self._gate.checkpoint_id = "checkpoint:advanced"
        self._gate.entered.set()
        if self._process_loss:
            raise _SimulatedProcessLoss()
        if self._block:
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
            checkpoint_id=self._gate.checkpoint_id,
        )


class _NoProbeRuntime:
    runtime_id = "claim-proof"
    capabilities = frozenset()

    def __init__(self) -> None:
        self.resume_calls = 0

    async def run(self, request):
        raise AssertionError("recovery must resume, not run")

    async def resume(self, request):
        self.resume_calls += 1
        return RuntimeResult(outcome=RuntimeOutcome.COMPLETED)

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        del task_id, thread_id
        return False


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
        assert completed.result["checkpoint_proven"] is True

    asyncio.run(scenario())


def test_direct_resume_claim_survives_checkpoint_advance_by_first_owner(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "explicit.db"
        store, queue, task_id = _active_session(path)
        gate = _SharedResumeGate()
        runtime_a = _ClaimedRuntime(
            gate,
            "process-a",
            advance_checkpoint=True,
        )
        runtime_b = _ClaimedRuntime(gate, "process-b", block=False)
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
        assert gate.checkpoint_id == "checkpoint:advanced"
        with pytest.raises(RuntimeRecoveryClaimConflict):
            await second.resume_saved(runtime_b, task_id=task_id)
        assert gate.calls == ["process-a"]

        claims = [
            record
            for record in IdempotencyLedger(store).list_for_task(task_id)
            if record.operation_type == "runtime.recovery_resume"
        ]
        assert len(claims) == 1
        assert claims[0].status == IdempotencyStatus.PENDING

        gate.release.set()
        result = await first_task
        assert result.outcome == RuntimeOutcome.COMPLETED
        assert gate.calls == ["process-a"]

    asyncio.run(scenario())


def test_durable_resume_requires_exact_checkpoint_probe_before_claim(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "no-probe.db"
        store, queue, task_id = _active_session(path)
        runtime = _NoProbeRuntime()
        coordinator = TaskRuntimeCoordinator(
            queue,
            AuditLog(store),
            recovery_owner_id="process-no-probe",
        )

        with pytest.raises(TypeError, match="RuntimeResumeProbePort"):
            await coordinator.resume_saved(runtime, task_id=task_id)

        assert runtime.resume_calls == 0
        assert RuntimeSessionStore(store).get(task_id) is not None
        assert queue.get(task_id).state == TaskState.RUNNING
        claims = [
            record
            for record in IdempotencyLedger(store).list_for_task(task_id)
            if record.operation_type == "runtime.recovery_resume"
        ]
        assert claims == []

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


def test_ready_resume_probe_rejects_noncanonical_checkpoint_identity() -> None:
    with pytest.raises(ValueError, match="checkpoint_id"):
        RuntimeResumeProbe(
            status=RuntimeResumeProbeStatus.READY,
            reason="invalid whitespace identity",
            checkpoint_id=" ",
        )
    with pytest.raises(ValueError, match="surrounding whitespace"):
        RuntimeResumeProbe(
            status=RuntimeResumeProbeStatus.READY,
            reason="invalid padded identity",
            checkpoint_id=" checkpoint-1 ",
        )
    with pytest.raises(TypeError, match="checkpoint_id"):
        RuntimeResumeProbe(
            status=RuntimeResumeProbeStatus.READY,
            reason="invalid non-string identity",
            checkpoint_id=1,
        )
