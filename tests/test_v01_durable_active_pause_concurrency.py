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
from nika_core.runtime.idempotency import IdempotencyLedger


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


class GatedCancelLedger(IdempotencyLedger):
    def __init__(
        self,
        store: SQLiteStore,
        *,
        reached_reservation: threading.Event,
        release_reservation: threading.Event,
    ) -> None:
        super().__init__(store)
        self.reached_reservation = reached_reservation
        self.release_reservation = release_reservation

    def reserve_with_connection(
        self,
        conn: sqlite3.Connection,
        *,
        operation_key: str,
        task_id: str,
        operation_type: str,
        input_fingerprint: str,
    ):
        if operation_type == "runtime.cancel":
            self.reached_reservation.set()
            assert self.release_reservation.wait(timeout=5)
        return super().reserve_with_connection(
            conn,
            operation_key=operation_key,
            task_id=task_id,
            operation_type=operation_type,
            input_fingerprint=input_fingerprint,
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


def test_cancel_authority_serializes_session_check_with_reservation(tmp_path) -> None:
    db_path = tmp_path / "Ніка cancel authority race.db"
    store = SQLiteStore(db_path)
    store.initialize()
    queue = TaskQueue(store)
    task = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "cancel authority race"},
    )
    queue.transition(task.task_id, TaskState.READY)
    queue.transition(task.task_id, TaskState.RUNNING)

    thread_id = "thread-cancel-authority"
    runtime = CountingResumeRuntime(threading.Barrier(1))
    reached_reservation = threading.Event()
    release_reservation = threading.Event()
    ledger = GatedCancelLedger(
        store,
        reached_reservation=reached_reservation,
        release_reservation=release_reservation,
    )
    coordinator = TaskRuntimeCoordinator(
        queue,
        AuditLog(store),
        idempotency=ledger,
    )
    coordinator.sessions.record_active(
        task_id=task.task_id,
        runtime_id=runtime.runtime_id,
        thread_id=thread_id,
        resume_token=thread_id,
    )

    def cancel() -> bool:
        return asyncio.run(
            coordinator.cancel(
                runtime,
                task_id=task.task_id,
                thread_id=thread_id,
            )
        )

    replacement_acquired = False
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(cancel)
        assert reached_reservation.wait(timeout=5)
        replacement = sqlite3.connect(db_path, timeout=0.05)
        try:
            replacement.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            assert "locked" in str(exc).lower()
        else:
            replacement_acquired = True
            replacement.execute(
                "UPDATE runtime_sessions SET thread_id = ? WHERE task_id = ?",
                ("thread-replaced-during-cancel", task.task_id),
            )
            replacement.commit()
        finally:
            replacement.close()
            release_reservation.set()
        assert future.result(timeout=5) is False

    assert replacement_acquired is False
    session = coordinator.sessions.get(task.task_id)
    assert session is not None
    assert session.thread_id == thread_id
    assert tuple(
        record
        for record in ledger.list_for_task(task.task_id)
        if record.operation_type == "runtime.cancel"
    ) == ()
