from __future__ import annotations

import asyncio

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.runtime.contracts import RuntimeCapability
from nika_core.runtime.coordinator import TaskRuntimeCoordinator
from nika_core.runtime.session_store import RuntimeSessionStore


class PauseThenInactiveRuntime:
    runtime_id = "qa.pause-then-inactive"
    capabilities = frozenset(
        {RuntimeCapability.DURABLE_RESUME, RuntimeCapability.CANCELLATION}
    )

    def __init__(self) -> None:
        self.cancel_calls = 0
        self.resume_calls = 0

    async def run(self, request):
        raise AssertionError(f"fresh run is outside this oracle: {request.task_id}")

    async def resume(self, request):
        self.resume_calls += 1
        raise AssertionError(f"cancelled paused task must never resume: {request.task_id}")

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        del task_id, thread_id
        self.cancel_calls += 1
        # The first call acknowledges the active RUNNING stop used by Pause.
        # After Pause is confirmed the runtime is inactive, so a second stop is false.
        return self.cancel_calls == 1


def test_explicit_cancel_of_confirmed_paused_task_is_terminal_without_second_runtime_stop(
    tmp_path,
) -> None:
    store = SQLiteStore(tmp_path / "Ніка pause then cancel.db")
    store.initialize()
    queue = TaskQueue(store)
    task = queue.create(
        workspace_id="default",
        agent_id="nika.default",
        payload={"command": "long resumable work"},
    )
    queue.transition(task.task_id, TaskState.READY)
    queue.transition(task.task_id, TaskState.RUNNING)

    thread_id = "thread-pause-then-cancel"
    RuntimeSessionStore(store).record_active(
        task_id=task.task_id,
        runtime_id=PauseThenInactiveRuntime.runtime_id,
        thread_id=thread_id,
        resume_token="checkpoint-pause-then-cancel",
    )
    runtime = PauseThenInactiveRuntime()
    coordinator = TaskRuntimeCoordinator(queue, AuditLog(store))

    assert asyncio.run(
        coordinator.pause(
            runtime,
            task_id=task.task_id,
            thread_id=thread_id,
        )
    )
    assert queue.get(task.task_id).state is TaskState.PAUSED
    assert runtime.cancel_calls == 1

    cancelled = asyncio.run(
        coordinator.cancel(
            runtime,
            task_id=task.task_id,
            thread_id=thread_id,
        )
    )

    assert cancelled is True
    assert runtime.cancel_calls == 1
    assert runtime.resume_calls == 0
    assert queue.get(task.task_id).state is TaskState.CANCELLED
    assert RuntimeSessionStore(SQLiteStore(store.path)).get(task.task_id) is None

    restarted_queue = TaskQueue(SQLiteStore(store.path))
    assert restarted_queue.get(task.task_id).state is TaskState.CANCELLED
