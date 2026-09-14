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
from nika_core.runtime.idempotency import IdempotencyLedger
from nika_core.runtime.recovery_claims import RECOVERY_RESUME_OPERATION_TYPE


class ProbeGateRuntime:
    runtime_id = "v01-pause-resume-control-race"
    capabilities = frozenset(
        {RuntimeCapability.DURABLE_RESUME, RuntimeCapability.CANCELLATION}
    )

    def __init__(self, *, gate_probe: bool = False) -> None:
        self.gate_probe = gate_probe
        self.probe_started = threading.Event()
        self.probe_release = threading.Event()
        if not gate_probe:
            self.probe_release.set()
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
        return False

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


def paused_task(store: SQLiteStore, runtime: ProbeGateRuntime) -> tuple[TaskQueue, str, str]:
    queue = TaskQueue(store)
    task = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "pause resume race"},
    )
    queue.transition(task.task_id, TaskState.READY)
    queue.transition(task.task_id, TaskState.RUNNING)
    queue.transition(task.task_id, TaskState.PAUSED)
    thread_id = "thread-pause-resume-race"
    TaskRuntimeCoordinator(queue, AuditLog(store)).sessions.record_result(
        task_id=task.task_id,
        runtime_id=runtime.runtime_id,
        thread_id=thread_id,
        result=RuntimeResult(
            outcome=RuntimeOutcome.PAUSED,
            resume_token=thread_id,
        ),
    )
    return queue, task.task_id, thread_id


def test_pause_reaffirmation_during_resume_probe_fences_stale_resume(tmp_path) -> None:
    async def scenario() -> None:
        store = SQLiteStore(tmp_path / "Ніка pause during resume probe.db")
        store.initialize()
        runtime = ProbeGateRuntime(gate_probe=True)
        queue, task_id, thread_id = paused_task(store, runtime)
        coordinator = TaskRuntimeCoordinator(queue, AuditLog(store))
        before = coordinator.sessions.get(task_id)
        assert before is not None

        resume = asyncio.create_task(coordinator.resume_saved(runtime, task_id=task_id))
        probe_started = await asyncio.to_thread(runtime.probe_started.wait, 2)
        assert probe_started

        assert await coordinator.pause(runtime, task_id=task_id, thread_id=thread_id)
        reaffirmed = coordinator.sessions.get(task_id)
        assert reaffirmed is not None
        assert reaffirmed.outcome is RuntimeOutcome.PAUSED
        assert reaffirmed.updated_at != before.updated_at
        assert runtime.cancel_calls == 0

        runtime.probe_release.set()
        with pytest.raises(ValueError, match="session changed before durable recovery claim"):
            await resume

        assert runtime.resume_calls == 0
        assert queue.get(task_id).state is TaskState.PAUSED
        recovery_records = tuple(
            record
            for record in IdempotencyLedger(store).list_for_task(task_id)
            if record.operation_type == RECOVERY_RESUME_OPERATION_TYPE
        )
        assert recovery_records == ()

    asyncio.run(scenario())


def test_pause_after_claim_before_running_releases_claim_and_wins(tmp_path) -> None:
    db_path = tmp_path / "Ніка pause after resume claim.db"
    store = SQLiteStore(db_path)
    store.initialize()
    runtime = ProbeGateRuntime()
    _queue, task_id, thread_id = paused_task(store, runtime)

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

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(resume)
        assert prepare_entered.wait(timeout=5)

        pending_before_pause = tuple(
            record
            for record in IdempotencyLedger(store).list_for_task(task_id)
            if record.operation_type == RECOVERY_RESUME_OPERATION_TYPE
        )
        assert len(pending_before_pause) == 1

        assert asyncio.run(
            pause_coordinator.pause(
                runtime,
                task_id=task_id,
                thread_id=thread_id,
            )
        )
        assert TaskQueue(store).get(task_id).state is TaskState.PAUSED
        assert runtime.cancel_calls == 0

        prepare_release.set()
        with pytest.raises(
            ValueError,
            match="changed after durable recovery claim and before resume",
        ):
            future.result(timeout=5)

    assert runtime.resume_calls == 0
    recovery_records = tuple(
        record
        for record in IdempotencyLedger(store).list_for_task(task_id)
        if record.operation_type == RECOVERY_RESUME_OPERATION_TYPE
    )
    assert recovery_records == ()

    completed = asyncio.run(
        TaskRuntimeCoordinator(TaskQueue(store), AuditLog(store)).resume_saved(
            runtime,
            task_id=task_id,
        )
    )
    assert completed.outcome is RuntimeOutcome.COMPLETED
    assert runtime.resume_calls == 1
    assert TaskQueue(store).get(task_id).state is TaskState.COMPLETED
