from __future__ import annotations

import asyncio
from contextlib import suppress

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
    RuntimeResumeProbe,
    RuntimeResumeProbeStatus,
    RuntimeResumeRequest,
)
from nika_core.runtime.coordinator import TaskRuntimeCoordinator
from nika_core.runtime.idempotency import (
    IdempotencyConflictError,
    IdempotencyLedger,
    IdempotencyStatus,
)
from nika_core.runtime.recovery import RecoveryDisposition, RuntimeRecoveryService
from nika_core.runtime.registry import RuntimeRegistry


class BlockingDurableRuntime:
    runtime_id = "v01-durable-active-pause"
    capabilities = frozenset(
        {RuntimeCapability.DURABLE_RESUME, RuntimeCapability.CANCELLATION}
    )

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.active: asyncio.Task[None] | None = None
        self.cancel_calls = 0
        self.resume_calls = 0

    @staticmethod
    def initial_resume_token(*, task_id: str, thread_id: str) -> str:
        del task_id
        return thread_id

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        del request
        active = asyncio.create_task(asyncio.Event().wait())
        self.active = active
        self.started.set()
        try:
            await active
        except asyncio.CancelledError:
            return RuntimeResult(outcome=RuntimeOutcome.CANCELLED)
        finally:
            if self.active is active:
                self.active = None
        return RuntimeResult(outcome=RuntimeOutcome.COMPLETED)

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        self.resume_calls += 1
        return RuntimeResult(
            outcome=RuntimeOutcome.COMPLETED,
            output={"continued_from": request.resume_token},
        )

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        del task_id, thread_id
        self.cancel_calls += 1
        active = self.active
        if active is None or active.done():
            return False
        active.cancel()
        with suppress(asyncio.CancelledError):
            await active
        return True

    async def probe_resume(
        self,
        *,
        task_id: str,
        thread_id: str,
        resume_token: str,
    ) -> RuntimeResumeProbe:
        del task_id, thread_id
        return RuntimeResumeProbe(
            status=RuntimeResumeProbeStatus.READY,
            reason="checkpoint ready",
            checkpoint_id=f"checkpoint:{resume_token}",
        )


class AckedExternalPauseRuntime(BlockingDurableRuntime):
    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        raise AssertionError(f"fresh run is not part of this scenario: {request.task_id}")

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        del task_id, thread_id
        self.cancel_calls += 1
        return True


class LateCompletionAfterPauseRuntime(BlockingDurableRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        del request
        self.started.set()
        await self.release.wait()
        return RuntimeResult(outcome=RuntimeOutcome.COMPLETED)

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        del task_id, thread_id
        self.cancel_calls += 1
        return True


class UncertainPauseRuntime(BlockingDurableRuntime):
    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        del task_id, thread_id
        self.cancel_calls += 1
        raise RuntimeError("pause acknowledgement unavailable")


def ready_task(store: SQLiteStore) -> tuple[TaskQueue, str]:
    queue = TaskQueue(store)
    task = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "long running work"},
    )
    queue.transition(task.task_id, TaskState.READY)
    return queue, task.task_id


def test_active_pause_survives_restart_until_explicit_resume(tmp_path) -> None:
    async def scenario() -> None:
        store = SQLiteStore(tmp_path / "Ніка durable active pause.db")
        store.initialize()
        queue, task_id = ready_task(store)
        runtime = BlockingDurableRuntime()
        coordinator = TaskRuntimeCoordinator(queue, AuditLog(store))
        thread_id = "thread-durable-pause"

        running = asyncio.create_task(
            coordinator.start(
                runtime,
                RuntimeRequest(task_id=task_id, thread_id=thread_id),
            )
        )
        await asyncio.wait_for(runtime.started.wait(), timeout=2)
        assert queue.get(task_id).state is TaskState.RUNNING

        assert await coordinator.pause(runtime, task_id=task_id, thread_id=thread_id)
        paused_result = await asyncio.wait_for(running, timeout=2)
        assert paused_result.outcome is RuntimeOutcome.PAUSED
        assert queue.get(task_id).state is TaskState.PAUSED
        assert coordinator.sessions.get(task_id).outcome is RuntimeOutcome.PAUSED

        assert await coordinator.pause(runtime, task_id=task_id, thread_id=thread_id)
        assert runtime.cancel_calls == 1

        recreated = TaskRuntimeCoordinator(TaskQueue(store), AuditLog(store))
        recreated_runtime = BlockingDurableRuntime()
        assert TaskQueue(store).get(task_id).state is TaskState.PAUSED
        completed = await recreated.resume_saved(recreated_runtime, task_id=task_id)
        assert completed.outcome is RuntimeOutcome.COMPLETED
        assert recreated_runtime.resume_calls == 1
        assert TaskQueue(store).get(task_id).state is TaskState.COMPLETED

    asyncio.run(scenario())


def test_explicit_cancel_of_confirmed_paused_task_is_terminal_without_second_runtime_stop(
    tmp_path,
) -> None:
    async def scenario() -> None:
        store = SQLiteStore(tmp_path / "Ніка pause then cancel.db")
        store.initialize()
        queue, task_id = ready_task(store)
        runtime = BlockingDurableRuntime()
        coordinator = TaskRuntimeCoordinator(queue, AuditLog(store))
        thread_id = "thread-pause-then-cancel"

        running = asyncio.create_task(
            coordinator.start(
                runtime,
                RuntimeRequest(task_id=task_id, thread_id=thread_id),
            )
        )
        await asyncio.wait_for(runtime.started.wait(), timeout=2)
        assert await coordinator.pause(runtime, task_id=task_id, thread_id=thread_id)
        paused_result = await asyncio.wait_for(running, timeout=2)
        assert paused_result.outcome is RuntimeOutcome.PAUSED
        assert queue.get(task_id).state is TaskState.PAUSED
        assert runtime.cancel_calls == 1

        assert await coordinator.cancel(runtime, task_id=task_id, thread_id=thread_id)
        assert runtime.cancel_calls == 1
        assert runtime.resume_calls == 0
        assert queue.get(task_id).state is TaskState.CANCELLED
        assert coordinator.sessions.get(task_id) is None

        cancel_records = tuple(
            record
            for record in IdempotencyLedger(store).list_for_task(task_id)
            if record.operation_type == "runtime.cancel"
        )
        assert len(cancel_records) == 1
        assert cancel_records[0].status is IdempotencyStatus.COMPLETED
        assert cancel_records[0].result == {
            "accepted": True,
            "runtime_call_skipped": True,
            "task_state": TaskState.CANCELLED.value,
        }

        restarted_queue = TaskQueue(SQLiteStore(store.path))
        assert restarted_queue.get(task_id).state is TaskState.CANCELLED

    asyncio.run(scenario())


def test_confirmed_pause_return_boundary_is_restart_stable(tmp_path) -> None:
    async def scenario() -> None:
        store = SQLiteStore(tmp_path / "Ніка confirmed pause restart.db")
        store.initialize()
        queue, task_id = ready_task(store)
        queue.transition(task_id, TaskState.RUNNING)
        runtime = AckedExternalPauseRuntime()
        thread_id = "thread-confirmed-pause"
        coordinator = TaskRuntimeCoordinator(queue, AuditLog(store))
        coordinator.sessions.record_active(
            task_id=task_id,
            runtime_id=runtime.runtime_id,
            thread_id=thread_id,
            resume_token=thread_id,
        )

        assert await coordinator.pause(runtime, task_id=task_id, thread_id=thread_id)
        pause_records = tuple(
            record
            for record in IdempotencyLedger(store).list_for_task(task_id)
            if record.operation_type == "runtime.pause"
        )
        assert len(pause_records) == 1
        assert pause_records[0].status is IdempotencyStatus.COMPLETED

        restarted_runtime = AckedExternalPauseRuntime()
        registry = RuntimeRegistry()
        registry.register(restarted_runtime)
        recovery = RuntimeRecoveryService(
            queue=TaskQueue(store),
            audit=AuditLog(store),
            runtimes=registry,
        )
        candidate = next(item for item in recovery.inspect() if item.task_id == task_id)
        assert candidate.disposition is RecoveryDisposition.MANUAL_RESUME
        assert candidate.unresolved_operation_keys == ()

    asyncio.run(scenario())


def test_late_non_cancelled_runtime_outcome_keeps_pause_uncertain(tmp_path) -> None:
    async def scenario() -> None:
        store = SQLiteStore(tmp_path / "Ніка late pause outcome.db")
        store.initialize()
        queue, task_id = ready_task(store)
        runtime = LateCompletionAfterPauseRuntime()
        coordinator = TaskRuntimeCoordinator(queue, AuditLog(store))
        thread_id = "thread-late-pause-outcome"

        running = asyncio.create_task(
            coordinator.start(
                runtime,
                RuntimeRequest(task_id=task_id, thread_id=thread_id),
            )
        )
        await asyncio.wait_for(runtime.started.wait(), timeout=2)
        assert await coordinator.pause(runtime, task_id=task_id, thread_id=thread_id)
        assert queue.get(task_id).state is TaskState.PAUSED

        runtime.release.set()
        result = await asyncio.wait_for(running, timeout=2)
        assert result.outcome is RuntimeOutcome.PAUSED
        assert queue.get(task_id).state is TaskState.PAUSED

        outcome_records = tuple(
            record
            for record in IdempotencyLedger(store).list_for_task(task_id)
            if record.operation_type == "runtime.pause.outcome"
        )
        assert len(outcome_records) == 1
        assert outcome_records[0].status is IdempotencyStatus.UNCERTAIN
        with pytest.raises(IdempotencyConflictError, match="pause is pending or uncertain"):
            await coordinator.resume_saved(runtime, task_id=task_id)
        assert runtime.resume_calls == 0

    asyncio.run(scenario())


def test_uncertain_pause_blocks_resume(tmp_path) -> None:
    async def scenario() -> None:
        store = SQLiteStore(tmp_path / "Ніка pause uncertain.db")
        store.initialize()
        queue, task_id = ready_task(store)
        runtime = UncertainPauseRuntime()
        coordinator = TaskRuntimeCoordinator(queue, AuditLog(store))
        thread_id = "thread-uncertain"

        queue.transition(task_id, TaskState.RUNNING)
        coordinator.sessions.record_active(
            task_id=task_id,
            runtime_id=runtime.runtime_id,
            thread_id=thread_id,
            resume_token=thread_id,
        )
        with pytest.raises(RuntimeError, match="acknowledgement unavailable"):
            await coordinator.pause(runtime, task_id=task_id, thread_id=thread_id)

        pause_records = tuple(
            record
            for record in IdempotencyLedger(store).list_for_task(task_id)
            if record.operation_type == "runtime.pause"
        )
        assert len(pause_records) == 1
        assert pause_records[0].status is IdempotencyStatus.UNCERTAIN
        with pytest.raises(IdempotencyConflictError, match="pause is pending or uncertain"):
            await coordinator.resume_saved(runtime, task_id=task_id)
        assert runtime.resume_calls == 0

    asyncio.run(scenario())
