from __future__ import annotations

import asyncio
import sqlite3
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
    RuntimeResumeRequest,
)
from nika_core.runtime.coordinator import TaskRuntimeCoordinator
from nika_core.scheduler import APSchedulerAdapter, ScheduledJob, ScheduledJobStore, TriggerKind


class _CountingResumeRuntime:
    runtime_id = "worker16-durable-pause-audit"
    capabilities = frozenset(
        {RuntimeCapability.DURABLE_RESUME, RuntimeCapability.CANCELLATION}
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.resume_calls = 0

    @staticmethod
    def initial_resume_token(*, task_id: str, thread_id: str) -> str:
        del task_id
        return thread_id

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        del request
        return RuntimeResult(outcome=RuntimeOutcome.COMPLETED)

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        with self._lock:
            self.resume_calls += 1
        return RuntimeResult(
            outcome=RuntimeOutcome.COMPLETED,
            output={"continued_from": request.resume_token},
        )

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        del task_id, thread_id
        return False


class _ReadyTransitionBarrierQueue(TaskQueue):
    """Force two independent resume callers to contend from the same PAUSED snapshot."""

    def __init__(self, store: SQLiteStore, barrier: threading.Barrier) -> None:
        super().__init__(store)
        self._barrier = barrier
        self._barrier_used = False

    def transition(self, task_id: str, target: TaskState) -> TaskState:
        if target == TaskState.READY and not self._barrier_used:
            if self.get(task_id).state == TaskState.PAUSED:
                self._barrier_used = True
                self._barrier.wait(timeout=10)
        return super().transition(task_id, target)


def _seed_paused_task(db_path) -> tuple[str, str]:
    store = SQLiteStore(db_path)
    store.initialize()
    queue = TaskQueue(store)
    task = queue.create(
        workspace_id="worker16",
        agent_id="audit",
        payload={"goal": "resume exactly once"},
    )
    queue.transition(task.task_id, TaskState.READY)
    queue.transition(task.task_id, TaskState.RUNNING)
    queue.transition(task.task_id, TaskState.PAUSED)
    thread_id = "worker16-pause-thread"
    TaskRuntimeCoordinator(queue, AuditLog(store)).sessions.record_result(
        task_id=task.task_id,
        runtime_id=_CountingResumeRuntime.runtime_id,
        thread_id=thread_id,
        result=RuntimeResult(
            outcome=RuntimeOutcome.PAUSED,
            resume_token=thread_id,
        ),
    )
    return task.task_id, thread_id


def test_concurrent_duplicate_resume_across_independent_coordinators_starts_one_continuation(
    tmp_path,
) -> None:
    db_path = tmp_path / "Ніка worker16 resume race.db"
    task_id, thread_id = _seed_paused_task(db_path)
    runtime = _CountingResumeRuntime()
    barrier = threading.Barrier(2)

    coordinators = tuple(
        TaskRuntimeCoordinator(
            _ReadyTransitionBarrierQueue(SQLiteStore(db_path), barrier),
            AuditLog(SQLiteStore(db_path)),
        )
        for _ in range(2)
    )

    def resume(index: int) -> RuntimeResult:
        return asyncio.run(coordinators[index].resume_saved(runtime, task_id=task_id))

    results: list[RuntimeResult] = []
    errors: list[BaseException] = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(resume, index) for index in range(2)]
        for future in futures:
            try:
                results.append(future.result())
            except BaseException as exc:  # capture the losing fail-closed caller for assertions
                errors.append(exc)

    assert len(results) == 1
    assert results[0].outcome == RuntimeOutcome.COMPLETED
    assert results[0].output == {"continued_from": thread_id}
    assert len(errors) == 1
    assert isinstance(errors[0], (ValueError, sqlite3.OperationalError))
    assert runtime.resume_calls == 1

    restarted_store = SQLiteStore(db_path)
    assert TaskQueue(restarted_store).get(task_id).state == TaskState.COMPLETED
    restarted = TaskRuntimeCoordinator(TaskQueue(restarted_store), AuditLog(restarted_store))
    assert restarted.sessions.get(task_id) is None
    with pytest.raises(KeyError, match="No resumable runtime session"):
        asyncio.run(restarted.resume_saved(runtime, task_id=task_id))
    assert runtime.resume_calls == 1


def test_terminal_completed_and_cancelled_states_reject_even_stale_paused_cursor(tmp_path) -> None:
    for terminal in (TaskState.COMPLETED, TaskState.CANCELLED):
        db_path = tmp_path / f"worker16 terminal {terminal.value}.db"
        task_id, _ = _seed_paused_task(db_path)
        store = SQLiteStore(db_path)
        queue = TaskQueue(store)

        if terminal == TaskState.COMPLETED:
            queue.transition(task_id, TaskState.READY)
            queue.transition(task_id, TaskState.RUNNING)
            queue.transition(task_id, TaskState.COMPLETED)
        else:
            queue.transition(task_id, TaskState.CANCELLED)

        runtime = _CountingResumeRuntime()
        restarted = TaskRuntimeCoordinator(TaskQueue(SQLiteStore(db_path)), AuditLog(SQLiteStore(db_path)))
        with pytest.raises(ValueError, match="Invalid task transition"):
            asyncio.run(restarted.resume_saved(runtime, task_id=task_id))

        assert TaskQueue(SQLiteStore(db_path)).get(task_id).state == terminal
        assert runtime.resume_calls == 0
        stale = restarted.sessions.get(task_id)
        assert stale is not None
        assert stale.outcome == RuntimeOutcome.PAUSED


def test_paused_durable_wait_preserves_intended_occurrence_and_suppresses_restart_dispatch(
    tmp_path,
) -> None:
    db_path = tmp_path / "Ніка worker16 durable wait.db"
    store = SQLiteStore(db_path)
    store.initialize()
    jobs = ScheduledJobStore(store)
    calls: list[dict[str, object]] = []

    def resolver(action_id: str):
        assert action_id == "external.effect"
        return calls.append

    intended_trigger = {"run_date": "2099-01-02T03:04:05+00:00"}
    intended_payload = {"batch": 2, "target": "next-intended-work"}
    adapter = APSchedulerAdapter(jobs, resolver)
    adapter.upsert(
        ScheduledJob(
            job_id="worker16-next-batch",
            action_id="external.effect",
            trigger_kind=TriggerKind.DATE,
            trigger=intended_trigger,
            payload=intended_payload,
        )
    )
    adapter.start()
    try:
        assert adapter.has_runtime_job("worker16-next-batch")
        adapter.pause("worker16-next-batch")
        adapter.pause("worker16-next-batch")
        paused = ScheduledJobStore(SQLiteStore(db_path)).get("worker16-next-batch")
        assert paused is not None
        assert paused.enabled is False
        assert paused.trigger == intended_trigger
        assert paused.payload == intended_payload
        assert not adapter.has_runtime_job("worker16-next-batch")
        adapter._dispatch("worker16-next-batch")
        assert calls == []
    finally:
        adapter.shutdown()

    restarted_jobs = ScheduledJobStore(SQLiteStore(db_path))
    restarted = APSchedulerAdapter(restarted_jobs, resolver)
    restarted.start()
    try:
        persisted = restarted_jobs.get("worker16-next-batch")
        assert persisted is not None
        assert persisted.enabled is False
        assert persisted.trigger == intended_trigger
        assert persisted.payload == intended_payload
        assert not restarted.has_runtime_job("worker16-next-batch")

        restarted._dispatch("worker16-next-batch")
        assert calls == []

        restarted.resume("worker16-next-batch")
        restarted.resume("worker16-next-batch")
        resumed = restarted_jobs.get("worker16-next-batch")
        assert resumed is not None
        assert resumed.enabled is True
        assert resumed.trigger == intended_trigger
        assert resumed.payload == intended_payload
        assert restarted.has_runtime_job("worker16-next-batch")

        restarted._dispatch("worker16-next-batch")
        assert calls == [intended_payload]
    finally:
        restarted.shutdown()
