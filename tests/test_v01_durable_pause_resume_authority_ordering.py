from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

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
from nika_core.runtime.recovery_claims import RECOVERY_RESUME_OPERATION_TYPE


class PauseGateRuntime:
    runtime_id = "v01-pause-resume-authority-ordering"
    capabilities = frozenset(
        {RuntimeCapability.DURABLE_RESUME, RuntimeCapability.CANCELLATION}
    )

    def __init__(self, *, gate_probe: bool = False) -> None:
        self.probe_started = threading.Event()
        self.probe_release = threading.Event()
        if not gate_probe:
            self.probe_release.set()
        self.cancel_started = threading.Event()
        self.cancel_release = threading.Event()
        self.resume_calls = 0
        self.cancel_calls = 0

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        raise AssertionError(f"fresh run is not part of this scenario: {request.task_id}")

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        self.resume_calls += 1
        return RuntimeResult(
            outcome=RuntimeOutcome.COMPLETED,
            output={"continued_from": request.resume_token},
        )

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        del task_id, thread_id
        self.cancel_calls += 1
        self.cancel_started.set()
        released = await asyncio.to_thread(self.cancel_release.wait, 5)
        if not released:
            raise AssertionError("cancel gate was not released")
        return True

    async def probe_resume(
        self,
        *,
        task_id: str,
        thread_id: str,
        resume_token: str,
    ) -> RuntimeResumeProbe:
        del task_id, thread_id
        self.probe_started.set()
        released = await asyncio.to_thread(self.probe_release.wait, 5)
        if not released:
            raise AssertionError("probe gate was not released")
        return RuntimeResumeProbe(
            status=RuntimeResumeProbeStatus.READY,
            reason="checkpoint ready",
            checkpoint_id=f"checkpoint:{resume_token}",
        )


class GatedPrepareCoordinator(TaskRuntimeCoordinator):
    def __init__(
        self,
        queue: TaskQueue,
        audit: AuditLog,
        *,
        prepare_entered: threading.Event,
        prepare_release: threading.Event,
    ) -> None:
        super().__init__(queue, audit)
        self.prepare_entered = prepare_entered
        self.prepare_release = prepare_release

    def _prepare_saved_resume_state(self, *args, **kwargs):
        self.prepare_entered.set()
        if not self.prepare_release.wait(timeout=5):
            raise AssertionError("prepare gate was not released")
        return super()._prepare_saved_resume_state(*args, **kwargs)


def active_task(store: SQLiteStore, runtime: PauseGateRuntime) -> tuple[TaskQueue, str, str]:
    queue = TaskQueue(store)
    task = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "pause resume authority ordering"},
    )
    queue.transition(task.task_id, TaskState.READY)
    queue.transition(task.task_id, TaskState.RUNNING)
    thread_id = "thread-pause-resume-authority"
    TaskRuntimeCoordinator(queue, AuditLog(store)).sessions.record_active(
        task_id=task.task_id,
        runtime_id=runtime.runtime_id,
        thread_id=thread_id,
        resume_token=thread_id,
    )
    return queue, task.task_id, thread_id


def pending_pause_records(store: SQLiteStore, task_id: str):
    return tuple(
        record
        for record in IdempotencyLedger(store).list_for_task(task_id)
        if record.operation_type == "runtime.pause"
        and record.status is IdempotencyStatus.PENDING
    )


def recovery_records(store: SQLiteStore, task_id: str):
    return tuple(
        record
        for record in IdempotencyLedger(store).list_for_task(task_id)
        if record.operation_type == RECOVERY_RESUME_OPERATION_TYPE
    )


def test_pending_pause_reserved_during_probe_fences_recovery_claim(tmp_path) -> None:
    async def scenario() -> None:
        store = SQLiteStore(tmp_path / "Ніка pause wins during recovery probe.db")
        store.initialize()
        runtime = PauseGateRuntime(gate_probe=True)
        queue, task_id, thread_id = active_task(store, runtime)
        resume_coordinator = TaskRuntimeCoordinator(
            TaskQueue(store),
            AuditLog(store),
        )
        pause_coordinator = TaskRuntimeCoordinator(
            TaskQueue(store),
            AuditLog(store),
        )

        resume = asyncio.create_task(resume_coordinator.resume_saved(runtime, task_id=task_id))
        assert await asyncio.to_thread(runtime.probe_started.wait, 2)

        pause = asyncio.create_task(
            pause_coordinator.pause(
                runtime,
                task_id=task_id,
                thread_id=thread_id,
            )
        )
        assert await asyncio.to_thread(runtime.cancel_started.wait, 2)
        assert len(pending_pause_records(store, task_id)) == 1
        assert queue.get(task_id).state is TaskState.RUNNING

        runtime.probe_release.set()
        with pytest.raises(
            IdempotencyConflictError,
            match="runtime pause is pending or uncertain",
        ):
            await resume

        assert runtime.resume_calls == 0
        assert recovery_records(store, task_id) == ()
        assert queue.get(task_id).state is TaskState.RUNNING

        runtime.cancel_release.set()
        assert await pause is True
        assert runtime.cancel_calls == 1
        assert queue.get(task_id).state is TaskState.PAUSED

    asyncio.run(scenario())


def test_pause_after_recovery_claim_revokes_unstarted_resume_effect(tmp_path) -> None:
    db_path = tmp_path / "Ніка pause wins after recovery claim.db"
    store = SQLiteStore(db_path)
    store.initialize()
    runtime = PauseGateRuntime()
    queue, task_id, thread_id = active_task(store, runtime)

    prepare_entered = threading.Event()
    prepare_release = threading.Event()
    resume_coordinator = GatedPrepareCoordinator(
        TaskQueue(SQLiteStore(db_path)),
        AuditLog(SQLiteStore(db_path)),
        prepare_entered=prepare_entered,
        prepare_release=prepare_release,
    )
    pause_coordinator = TaskRuntimeCoordinator(
        TaskQueue(SQLiteStore(db_path)),
        AuditLog(SQLiteStore(db_path)),
    )

    def resume() -> RuntimeResult:
        return asyncio.run(resume_coordinator.resume_saved(runtime, task_id=task_id))

    def pause() -> bool:
        return asyncio.run(
            pause_coordinator.pause(
                runtime,
                task_id=task_id,
                thread_id=thread_id,
            )
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        resume_future = pool.submit(resume)
        assert prepare_entered.wait(timeout=5)
        claimed = recovery_records(store, task_id)
        assert len(claimed) == 1
        assert claimed[0].status is IdempotencyStatus.PENDING

        pause_future = pool.submit(pause)
        assert runtime.cancel_started.wait(timeout=5)
        assert len(pending_pause_records(store, task_id)) == 1

        prepare_release.set()
        with pytest.raises(
            IdempotencyConflictError,
            match="runtime pause is pending or uncertain",
        ):
            resume_future.result(timeout=5)

        assert runtime.resume_calls == 0
        assert recovery_records(store, task_id) == ()
        assert queue.get(task_id).state is TaskState.RUNNING

        runtime.cancel_release.set()
        assert pause_future.result(timeout=5) is True

    assert runtime.cancel_calls == 1
    assert queue.get(task_id).state is TaskState.PAUSED
