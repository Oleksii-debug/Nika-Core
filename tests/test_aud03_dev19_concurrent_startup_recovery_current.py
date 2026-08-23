from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

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
from nika_core.runtime.coordinator import TaskRuntimeCoordinator
from nika_core.runtime.idempotency import IdempotencyLedger, IdempotencyStatus
from nika_core.runtime.recovery import RuntimeRecoveryService
from nika_core.runtime.registry import RuntimeRegistry
from nika_core.runtime.session_store import RuntimeSessionStore


@dataclass
class _Gate:
    calls: list[str] = field(default_factory=list)
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)


class _Runtime:
    runtime_id = "aud03-recovery-claim"
    capabilities = frozenset()

    def __init__(self, gate: _Gate, owner: str) -> None:
        self._gate = gate
        self._owner = owner

    async def run(self, request):
        raise AssertionError("restart recovery must resume, never run")

    async def resume(self, request):
        self._gate.calls.append(self._owner)
        self._gate.entered.set()
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


def _service(path: Path, gate: _Gate, owner: str) -> RuntimeRecoveryService:
    store = SQLiteStore(path)
    store.initialize()
    queue = TaskQueue(store)
    registry = RuntimeRegistry()
    registry.register(_Runtime(gate, owner))
    return RuntimeRecoveryService(
        queue=queue,
        audit=AuditLog(store),
        runtimes=registry,
        coordinator=TaskRuntimeCoordinator(
            queue,
            AuditLog(store),
            recovery_owner_id=owner,
        ),
        sessions=RuntimeSessionStore(store),
        idempotency=IdempotencyLedger(store),
    )


def test_two_restart_owners_cannot_enter_resume_for_one_checkpoint(tmp_path: Path) -> None:
    async def scenario() -> None:
        path = tmp_path / "nika.db"
        store = SQLiteStore(path)
        store.initialize()
        queue = TaskQueue(store)
        task = queue.create(workspace_id="workspace", agent_id="agent")
        queue.transition(task.task_id, TaskState.READY)
        queue.transition(task.task_id, TaskState.RUNNING)
        RuntimeSessionStore(store).record_active(
            task_id=task.task_id,
            runtime_id="aud03-recovery-claim",
            thread_id="thread-shared",
            resume_token="thread-shared",
        )

        gate = _Gate()
        first = _service(path, gate, "process-a")
        second = _service(path, gate, "process-b")

        first_task = asyncio.create_task(first.resume_safe_crash_sessions(max_count=1))
        await asyncio.wait_for(gate.entered.wait(), timeout=2)
        second_result = await second.resume_safe_crash_sessions(max_count=1)

        assert gate.calls == ["process-a"]
        assert len(second_result) <= 1
        if second_result:
            assert not second_result[0].succeeded

        claims = [
            item
            for item in IdempotencyLedger(store).list_for_task(task.task_id)
            if item.operation_type == "runtime.recovery_resume"
        ]
        assert len(claims) == 1
        assert claims[0].status is IdempotencyStatus.PENDING

        gate.release.set()
        first_result = await asyncio.wait_for(first_task, timeout=2)
        assert len(first_result) == 1
        assert first_result[0].succeeded
        assert gate.calls == ["process-a"]
        assert IdempotencyLedger(store).require(claims[0].operation_key).status is (
            IdempotencyStatus.COMPLETED
        )

    asyncio.run(scenario())
