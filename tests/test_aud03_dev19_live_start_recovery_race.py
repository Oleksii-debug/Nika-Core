from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from pathlib import Path

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.runtime.contracts import (
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResult,
    RuntimeResumeProbe,
    RuntimeResumeProbeStatus,
)
from nika_core.runtime.coordinator import TaskRuntimeCoordinator
from nika_core.runtime.idempotency import IdempotencyLedger
from nika_core.runtime.recovery import RuntimeRecoveryService
from nika_core.runtime.registry import RuntimeRegistry
from nika_core.runtime.session_store import RuntimeSessionStore


@dataclass
class _Gate:
    calls: list[str] = field(default_factory=list)
    live_run_entered: asyncio.Event = field(default_factory=asyncio.Event)
    release_live_run: asyncio.Event = field(default_factory=asyncio.Event)


class _Runtime:
    runtime_id = "aud03-live-start"
    capabilities = frozenset()

    def __init__(self, gate: _Gate, owner: str) -> None:
        self._gate = gate
        self._owner = owner

    def initial_resume_token(self, *, task_id: str, thread_id: str) -> str:
        del task_id
        return thread_id

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        self._gate.calls.append(f"{self._owner}:run")
        self._gate.live_run_entered.set()
        await self._gate.release_live_run.wait()
        return RuntimeResult(
            outcome=RuntimeOutcome.COMPLETED,
            output={"owner": self._owner, "task_id": request.task_id},
        )

    async def resume(self, request) -> RuntimeResult:
        self._gate.calls.append(f"{self._owner}:resume")
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
            reason="the live runtime has already established a readable checkpoint",
            checkpoint_id=f"checkpoint:{thread_id}",
        )


def _recovery(path: Path, gate: _Gate) -> RuntimeRecoveryService:
    store = SQLiteStore(path)
    store.initialize()
    queue = TaskQueue(store)
    registry = RuntimeRegistry()
    registry.register(_Runtime(gate, "startup-recovery"))
    return RuntimeRecoveryService(
        queue=queue,
        audit=AuditLog(store),
        runtimes=registry,
        coordinator=TaskRuntimeCoordinator(
            queue,
            AuditLog(store),
            recovery_owner_id="startup-recovery-owner",
        ),
        sessions=RuntimeSessionStore(store),
        idempotency=IdempotencyLedger(store),
    )


def test_startup_recovery_cannot_resume_while_original_durable_run_is_live(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        path = tmp_path / "nika.db"
        store = SQLiteStore(path)
        store.initialize()
        queue = TaskQueue(store)
        task = queue.create(workspace_id="workspace", agent_id="agent")
        queue.transition(task.task_id, TaskState.READY)

        gate = _Gate()
        live_runtime = _Runtime(gate, "live-owner")
        live_coordinator = TaskRuntimeCoordinator(
            queue,
            AuditLog(store),
            recovery_owner_id="live-owner",
        )
        live_task = asyncio.create_task(
            live_coordinator.start(
                live_runtime,
                RuntimeRequest(
                    task_id=task.task_id,
                    thread_id="shared-thread",
                ),
            )
        )
        await asyncio.wait_for(gate.live_run_entered.wait(), timeout=2)

        try:
            record = RuntimeSessionStore(store).get(task.task_id)
            assert record is not None
            assert record.is_active
            assert queue.get(task.task_id).state == TaskState.RUNNING

            recovery = _recovery(path, gate)
            result = await asyncio.wait_for(
                recovery.resume_safe_crash_sessions(max_count=1),
                timeout=2,
            )

            assert not any(call.endswith(":resume") for call in gate.calls), (
                "startup recovery entered resume while the original durable run was still live: "
                f"{gate.calls}"
            )
            assert not any(item.succeeded for item in result)
            assert gate.calls == ["live-owner:run"]
        finally:
            gate.release_live_run.set()
            with contextlib.suppress(Exception):
                await live_task

    asyncio.run(scenario())
