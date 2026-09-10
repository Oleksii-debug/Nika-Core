from __future__ import annotations

import asyncio

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.runtime.contracts import RuntimeCapability, RuntimeRequest, RuntimeResult, RuntimeResumeRequest
from nika_core.runtime.recovery import RecoveryDisposition, RuntimeRecoveryService
from nika_core.runtime.registry import RuntimeRegistry
from nika_core.runtime.session_store import RuntimeSessionStore


class _NoEffectRuntime:
    runtime_id = "retry-restart-fail-closed"
    capabilities = frozenset({RuntimeCapability.DURABLE_RESUME})

    def __init__(self) -> None:
        self.run_calls = 0
        self.resume_calls = 0

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        self.run_calls += 1
        raise AssertionError("crash-left RETRYING must not redispatch fresh work")

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        self.resume_calls += 1
        raise AssertionError("crash-left RETRYING must not resume without durable retry authority")

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        del task_id, thread_id
        return False


def test_crash_left_retrying_is_repeatably_fail_closed_without_retry_authority(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    queue = TaskQueue(store)
    task = queue.create(workspace_id="test", agent_id="agent")
    queue.transition(task.task_id, TaskState.READY)
    queue.transition(task.task_id, TaskState.RUNNING)
    RuntimeSessionStore(store).record_active(
        task_id=task.task_id,
        runtime_id="retry-restart-fail-closed",
        thread_id="thread-retrying",
        resume_token="thread-retrying",
    )
    queue.transition(task.task_id, TaskState.RETRYING)

    runtime = _NoEffectRuntime()
    registry = RuntimeRegistry()
    registry.register(runtime)

    # Recreate the recovery service repeatedly to model repeated process restarts.  Until the
    # canonical durable session carries retry-number + not-before authority, RETRYING must never
    # become executable merely because another process started.
    for _ in range(3):
        recovery = RuntimeRecoveryService(
            queue=queue,
            audit=AuditLog(store),
            runtimes=registry,
        )
        candidate = recovery.inspect()[0]
        assert candidate.task_state == TaskState.RETRYING
        assert candidate.disposition == RecoveryDisposition.INCONSISTENT_STATE
        assert "retry attempt/not-before" in candidate.reason
        assert asyncio.run(recovery.resume_safe_crash_sessions(max_count=1)) == ()

        with store.connection() as conn:
            row = conn.execute(
                "SELECT state FROM tasks WHERE task_id = ?", (task.task_id,)
            ).fetchone()
        assert TaskState(row["state"]) == TaskState.RETRYING

    assert runtime.run_calls == 0
    assert runtime.resume_calls == 0
