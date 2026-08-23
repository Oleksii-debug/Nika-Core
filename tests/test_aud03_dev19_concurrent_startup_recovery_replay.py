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
from nika_core.runtime.recovery import RuntimeRecoveryService
from nika_core.runtime.registry import RuntimeRegistry
from nika_core.runtime.session_store import RuntimeSessionStore


@dataclass
class _SharedResumeGate:
    calls: list[str] = field(default_factory=list)
    both_entered: asyncio.Event = field(default_factory=asyncio.Event)


class _ProcessLikeRuntime:
    runtime_id = "aud03-recoverable"
    capabilities = frozenset()

    def __init__(self, gate: _SharedResumeGate, instance_id: str) -> None:
        self._gate = gate
        self._instance_id = instance_id

    async def run(self, request):
        raise AssertionError("startup recovery must resume, not run")

    async def resume(self, request):
        self._gate.calls.append(self._instance_id)
        if len(self._gate.calls) >= 2:
            self._gate.both_entered.set()
        await asyncio.wait_for(self._gate.both_entered.wait(), timeout=1.0)
        return RuntimeResult(
            outcome=RuntimeOutcome.COMPLETED,
            output={"resumed_by": self._instance_id, "task_id": request.task_id},
        )

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        del task_id, thread_id
        return False

    async def probe_resume(self, *, task_id: str, thread_id: str, resume_token: str):
        del task_id
        assert resume_token == thread_id
        return RuntimeResumeProbe(
            status=RuntimeResumeProbeStatus.READY,
            reason="shared durable checkpoint exists",
            checkpoint_id=f"checkpoint:{thread_id}",
        )


def _recovery(path: Path, gate: _SharedResumeGate, instance_id: str) -> RuntimeRecoveryService:
    store = SQLiteStore(path)
    store.initialize()
    queue = TaskQueue(store)
    runtimes = RuntimeRegistry()
    runtimes.register(_ProcessLikeRuntime(gate, instance_id))
    return RuntimeRecoveryService(
        queue=queue,
        audit=AuditLog(store),
        runtimes=runtimes,
        sessions=RuntimeSessionStore(store),
    )


def test_two_startup_recovery_owners_cannot_resume_same_checkpoint_concurrently(
    tmp_path: Path,
) -> None:
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
            runtime_id="aud03-recoverable",
            thread_id="thread-shared",
            resume_token="thread-shared",
        )

        gate = _SharedResumeGate()
        first = _recovery(path, gate, "process-a")
        second = _recovery(path, gate, "process-b")
        results = await asyncio.gather(
            first.resume_safe_crash_sessions(max_count=1),
            second.resume_safe_crash_sessions(max_count=1),
            return_exceptions=True,
        )

        assert results
        assert gate.calls == ["process-a"] or gate.calls == ["process-b"]

    asyncio.run(scenario())
