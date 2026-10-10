from __future__ import annotations

import asyncio

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.runtime.contracts import (
    RuntimeCapability,
    RuntimeOutcome,
    RuntimeResult,
    RuntimeResumeProbe,
    RuntimeResumeProbeStatus,
)
from nika_core.runtime.coordinator import TaskRuntimeCoordinator
from nika_core.runtime.idempotency import IdempotencyLedger, IdempotencyStatus
from nika_core.runtime.recovery import RecoveryDisposition, RuntimeRecoveryService
from nika_core.runtime.registry import RuntimeRegistry
from nika_core.runtime.session_store import RuntimeSessionStore


class AckedExternalPauseRuntime:
    runtime_id = "qa.acked-external-pause"
    capabilities = frozenset(
        {RuntimeCapability.DURABLE_RESUME, RuntimeCapability.CANCELLATION}
    )

    def __init__(self) -> None:
        self.cancel_calls = 0
        self.resume_calls = 0

    async def run(self, request):
        raise AssertionError(f"fresh run is not part of this oracle: {request.task_id}")

    async def resume(self, request):
        self.resume_calls += 1
        return RuntimeResult(outcome=RuntimeOutcome.COMPLETED)

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        del task_id, thread_id
        self.cancel_calls += 1
        # Model an out-of-process runtime that has synchronously acknowledged the stop.
        # There is deliberately no in-process coordinator.start() coroutine available to
        # call _finish() after pause() returns; a crash at this boundary must still be safe.
        return True

    async def probe_resume(self, *, task_id: str, thread_id: str, resume_token: str):
        return RuntimeResumeProbe(
            status=RuntimeResumeProbeStatus.READY,
            reason="qa durable checkpoint exists",
            checkpoint_id=f"qa:{task_id}:{thread_id}:{resume_token}",
        )


def test_confirmed_pause_return_boundary_is_restart_stable(tmp_path) -> None:
    database = tmp_path / "Ніка підтверджений pause crash.db"
    store = SQLiteStore(database)
    store.initialize()
    queue = TaskQueue(store)
    audit = AuditLog(store)
    runtime = AckedExternalPauseRuntime()

    task = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "long durable operation"},
    )
    queue.transition(task.task_id, TaskState.READY)
    queue.transition(task.task_id, TaskState.RUNNING)
    thread_id = "thread-confirmed-pause"
    RuntimeSessionStore(store).record_active(
        task_id=task.task_id,
        runtime_id=runtime.runtime_id,
        thread_id=thread_id,
        resume_token="checkpoint-confirmed-pause",
    )

    paused = asyncio.run(
        TaskRuntimeCoordinator(queue, audit).pause(
            runtime,
            task_id=task.task_id,
            thread_id=thread_id,
        )
    )

    assert paused is True
    assert runtime.cancel_calls == 1
    assert queue.get(task.task_id).state is TaskState.PAUSED
    session = RuntimeSessionStore(store).get(task.task_id)
    assert session is not None
    assert session.outcome is RuntimeOutcome.PAUSED

    # Simulate immediate process loss after pause() returned success: reconstruct every
    # Nika service from the same durable database before any old coroutine can finalize.
    restarted_store = SQLiteStore(database)
    restarted_queue = TaskQueue(restarted_store)
    restarted_runtime = AckedExternalPauseRuntime()
    runtimes = RuntimeRegistry()
    runtimes.register(restarted_runtime)
    recovery = RuntimeRecoveryService(
        queue=restarted_queue,
        audit=AuditLog(restarted_store),
        runtimes=runtimes,
        sessions=RuntimeSessionStore(restarted_store),
        idempotency=IdempotencyLedger(restarted_store),
    )

    candidates = recovery.inspect()
    candidate = next(item for item in candidates if item.task_id == task.task_id)
    assert candidate.disposition is RecoveryDisposition.MANUAL_RESUME
    assert candidate.unresolved_operation_keys == ()

    pause_records = tuple(
        item
        for item in IdempotencyLedger(restarted_store).list_for_task(task.task_id)
        if item.operation_type == "runtime.pause"
    )
    assert len(pause_records) == 1
    assert pause_records[0].status is IdempotencyStatus.COMPLETED
    assert restarted_runtime.resume_calls == 0
