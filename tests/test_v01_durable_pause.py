from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta

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


class _BlockingDurableRuntime:
    runtime_id = "v01-durable-pause"
    capabilities = frozenset(
        {RuntimeCapability.DURABLE_RESUME, RuntimeCapability.CANCELLATION}
    )

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self._active: asyncio.Task[None] | None = None
        self.cancel_calls = 0
        self.resume_calls = 0
        self.effect_starts = 0

    @staticmethod
    def initial_resume_token(*, task_id: str, thread_id: str) -> str:
        del task_id
        return thread_id

    async def _long_effect(self) -> None:
        self.effect_starts += 1
        self.started.set()
        await asyncio.Event().wait()

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        del request
        invocation = asyncio.create_task(self._long_effect())
        self._active = invocation
        try:
            await invocation
        except asyncio.CancelledError:
            return RuntimeResult(outcome=RuntimeOutcome.CANCELLED)
        finally:
            if self._active is invocation:
                self._active = None
        return RuntimeResult(outcome=RuntimeOutcome.COMPLETED)

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        self.resume_calls += 1
        return RuntimeResult(
            outcome=RuntimeOutcome.COMPLETED,
            output={"resumed_from": request.resume_token},
        )

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        del task_id, thread_id
        self.cancel_calls += 1
        active = self._active
        if active is None or active.done():
            return False
        active.cancel()
        with suppress(asyncio.CancelledError):
            await active
        return True


def _ready_task(tmp_path) -> tuple[SQLiteStore, TaskQueue, str]:
    store = SQLiteStore(tmp_path / "Ніка pause state.db")
    store.initialize()
    queue = TaskQueue(store)
    task = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "long running work"},
    )
    queue.transition(task.task_id, TaskState.READY)
    return store, queue, task.task_id


def test_active_pause_is_durable_restart_safe_and_explicitly_resumed(tmp_path) -> None:
    async def scenario() -> None:
        store, queue, task_id = _ready_task(tmp_path)
        audit = AuditLog(store)
        runtime = _BlockingDurableRuntime()
        coordinator = TaskRuntimeCoordinator(queue, audit)
        thread_id = "thread-durable-pause"

        running = asyncio.create_task(
            coordinator.start(runtime, RuntimeRequest(task_id=task_id, thread_id=thread_id))
        )
        await asyncio.wait_for(runtime.started.wait(), timeout=2)
        assert queue.get(task_id).state == TaskState.RUNNING
        assert runtime.effect_starts == 1

        assert await coordinator.pause(runtime, task_id=task_id, thread_id=thread_id)
        paused_result = await asyncio.wait_for(running, timeout=2)
        assert paused_result.outcome == RuntimeOutcome.PAUSED
        assert queue.get(task_id).state == TaskState.PAUSED

        persisted = coordinator.sessions.get(task_id)
        assert persisted is not None
        assert persisted.outcome == RuntimeOutcome.PAUSED
        assert persisted.resume_token == thread_id
        assert runtime.cancel_calls == 1

        effect_count_at_confirmation = runtime.effect_starts
        await asyncio.sleep(0)
        assert runtime.effect_starts == effect_count_at_confirmation

        assert await coordinator.pause(runtime, task_id=task_id, thread_id=thread_id)
        assert runtime.cancel_calls == 1

        recreated_queue = TaskQueue(store)
        recreated = TaskRuntimeCoordinator(recreated_queue, AuditLog(store))
        recreated_runtime = _BlockingDurableRuntime()

        assert recreated_queue.get(task_id).state == TaskState.PAUSED
        assert any(
            item.task_id == task_id and item.state == TaskState.PAUSED
            for item in recreated_queue.list_recent(limit=50)
        )
        recreated_session = recreated.sessions.get(task_id)
        assert recreated_session is not None
        assert recreated_session.outcome == RuntimeOutcome.PAUSED
        assert recreated_runtime.resume_calls == 0

        assert await recreated.pause(
            recreated_runtime,
            task_id=task_id,
            thread_id=thread_id,
        )
        assert recreated_runtime.cancel_calls == 0
        assert recreated_queue.get(task_id).state == TaskState.PAUSED

        completed = await recreated.resume_saved(recreated_runtime, task_id=task_id)
        assert completed.outcome == RuntimeOutcome.COMPLETED
        assert completed.output == {"resumed_from": thread_id}
        assert recreated_runtime.resume_calls == 1
        assert recreated_queue.get(task_id).state == TaskState.COMPLETED
        assert recreated.sessions.get(task_id) is None

        with pytest.raises(KeyError, match="No resumable runtime session"):
            await recreated.resume_saved(recreated_runtime, task_id=task_id)
        assert recreated_runtime.resume_calls == 1

        event_types = {
            item.event_type
            for item in AuditLog(store).list_for(entity_type="task", entity_id=task_id)
        }
        assert "runtime.pause_requested" in event_types
        assert "runtime.pause_confirmed" in event_types
        assert "runtime.finished_after_pause" in event_types

    asyncio.run(scenario())


def test_active_pause_fails_closed_without_durable_runtime_session(tmp_path) -> None:
    async def scenario() -> None:
        _, queue, task_id = _ready_task(tmp_path)
        queue.transition(task_id, TaskState.RUNNING)
        runtime = _BlockingDurableRuntime()
        coordinator = TaskRuntimeCoordinator(queue, AuditLog(queue.store))

        with pytest.raises(ValueError, match="durable runtime session"):
            await coordinator.pause(runtime, task_id=task_id, thread_id="missing-session")
        assert runtime.cancel_calls == 0
        assert queue.get(task_id).state == TaskState.RUNNING

    asyncio.run(scenario())


def test_scheduler_pause_is_durable_idempotent_and_blocks_queued_dispatch(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "Ніка scheduled pause.db")
    store.initialize()
    calls: list[dict[str, object]] = []

    def resolver(action_id: str):
        assert action_id == "external.effect"
        return calls.append

    run_at = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    adapter = APSchedulerAdapter(ScheduledJobStore(store), resolver)
    adapter.upsert(
        ScheduledJob(
            job_id="future-effect",
            action_id="external.effect",
            trigger_kind=TriggerKind.DATE,
            trigger={"run_date": run_at},
            payload={"value": 1},
        )
    )
    adapter.start()
    try:
        assert adapter.has_runtime_job("future-effect")
        adapter.pause("future-effect")
        adapter.pause("future-effect")
        assert not adapter.has_runtime_job("future-effect")
        adapter._dispatch("future-effect")
        assert calls == []
    finally:
        adapter.shutdown()

    restarted = APSchedulerAdapter(ScheduledJobStore(store), resolver)
    restarted.start()
    try:
        assert not restarted.has_runtime_job("future-effect")
        restarted._dispatch("future-effect")
        assert calls == []

        restarted.pause("future-effect")
        restarted.pause("future-effect")
        assert not restarted.has_runtime_job("future-effect")

        restarted.resume("future-effect")
        restarted.resume("future-effect")
        assert restarted.has_runtime_job("future-effect")

        restarted.pause("future-effect")
        assert not restarted.has_runtime_job("future-effect")
        restarted._dispatch("future-effect")
        assert calls == []
    finally:
        restarted.shutdown()
