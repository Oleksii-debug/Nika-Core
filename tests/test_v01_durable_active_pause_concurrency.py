from __future__ import annotations

import asyncio
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

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
from nika_core.runtime.coordinator import RuntimeRecoveryClaimConflict, TaskRuntimeCoordinator


class CountingResumeRuntime:
    runtime_id = "v01-pause-resume-race"
    capabilities = frozenset(
        {RuntimeCapability.DURABLE_RESUME, RuntimeCapability.CANCELLATION}
    )

    def __init__(self, barrier: threading.Barrier) -> None:
        self.barrier = barrier
        self.lock = threading.Lock()
        self.resume_calls = 0

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        del request
        return RuntimeResult(outcome=RuntimeOutcome.COMPLETED)

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        del task_id, thread_id
        return False

    async def probe_resume(
        self,
        *,
        task_id: str,
        thread_id: str,
        resume_token: str,
    ) -> RuntimeResumeProbe:
        del task_id, thread_id
        self.barrier.wait(timeout=5)
        return RuntimeResumeProbe(
            status=RuntimeResumeProbeStatus.READY,
            reason="checkpoint ready",
            checkpoint_id=f"checkpoint:{resume_token}",
        )

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        with self.lock:
            self.resume_calls += 1
        return RuntimeResult(
            outcome=RuntimeOutcome.COMPLETED,
            output={"continued_from": request.resume_token},
        )


def test_two_independent_resume_callers_start_one_continuation(tmp_path) -> None:
    db_path = tmp_path / "Ніка pause resume race.db"
    store = SQLiteStore(db_path)
    store.initialize()
    queue = TaskQueue(store)
    task = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "resume race"},
    )
    queue.transition(task.task_id, TaskState.READY)
    queue.transition(task.task_id, TaskState.RUNNING)
    queue.transition(task.task_id, TaskState.PAUSED)
    thread_id = "thread-race"

    TaskRuntimeCoordinator(queue, AuditLog(store)).sessions.record_result(
        task_id=task.task_id,
        runtime_id=CountingResumeRuntime.runtime_id,
        thread_id=thread_id,
        result=RuntimeResult(
            outcome=RuntimeOutcome.PAUSED,
            resume_token=thread_id,
        ),
    )

    barrier = threading.Barrier(2)
    runtime = CountingResumeRuntime(barrier)
    coordinators = tuple(
        TaskRuntimeCoordinator(
            TaskQueue(SQLiteStore(db_path)),
            AuditLog(SQLiteStore(db_path)),
        )
        for _ in range(2)
    )

    def resume(index: int) -> RuntimeResult:
        return asyncio.run(coordinators[index].resume_saved(runtime, task_id=task.task_id))

    results: list[RuntimeResult] = []
    errors: list[Exception] = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(resume, index) for index in range(2)]
        for future in futures:
            try:
                results.append(future.result())
            except (ValueError, RuntimeRecoveryClaimConflict, sqlite3.OperationalError) as exc:
                errors.append(exc)

    assert len(results) == 1
    assert results[0].outcome is RuntimeOutcome.COMPLETED
    assert len(errors) == 1
    assert runtime.resume_calls == 1
    assert TaskQueue(store).get(task.task_id).state is TaskState.COMPLETED
