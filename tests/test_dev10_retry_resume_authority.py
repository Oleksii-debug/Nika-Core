from __future__ import annotations

import asyncio

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.runtime.contracts import (
    RuntimeCapability,
    RuntimeErrorCode,
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResult,
    RuntimeResumeProbe,
    RuntimeResumeProbeStatus,
    RuntimeResumeRequest,
)
from nika_core.runtime.coordinator import TaskRuntimeCoordinator
from nika_core.runtime.recovery import RecoveryDisposition, RuntimeRecoveryService
from nika_core.runtime.registry import RuntimeRegistry
from nika_core.runtime.retry import RetryPolicy
from nika_core.runtime.session_store import RuntimeSessionStore


class _RetryDispatchRuntime:
    runtime_id = "retry-authority-proof"
    capabilities = frozenset({RuntimeCapability.DURABLE_RESUME})

    def __init__(self, failure_token: object) -> None:
        self.failure_token = failure_token
        self.run_calls = 0
        self.resume_calls = 0

    @staticmethod
    def initial_resume_token(*, task_id: str, thread_id: str) -> str:
        del task_id
        return thread_id

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        self.run_calls += 1
        if self.run_calls == 1:
            return RuntimeResult(
                outcome=RuntimeOutcome.FAILED,
                error="temporary provider failure",
                error_code=RuntimeErrorCode.TRANSIENT,
                resume_token=self.failure_token,  # type: ignore[arg-type]
            )
        return RuntimeResult(outcome=RuntimeOutcome.COMPLETED, output={"fresh": True})

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        self.resume_calls += 1
        return RuntimeResult(outcome=RuntimeOutcome.COMPLETED, output={"resumed": True})

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
        del task_id
        assert resume_token == thread_id
        return RuntimeResumeProbe(
            status=RuntimeResumeProbeStatus.READY,
            reason="retry checkpoint exists",
            checkpoint_id=f"checkpoint:{thread_id}",
        )


class _RecoveryRuntime:
    runtime_id = "retry-recovery-proof"
    capabilities = frozenset({RuntimeCapability.DURABLE_RESUME})

    def __init__(self) -> None:
        self.run_calls = 0
        self.resume_calls = 0

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        self.run_calls += 1
        raise AssertionError("restart recovery must resume, not execute fresh")

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        self.resume_calls += 1
        return RuntimeResult(outcome=RuntimeOutcome.COMPLETED, output={"recovered": True})

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
        del task_id
        assert resume_token == thread_id
        return RuntimeResumeProbe(
            status=RuntimeResumeProbeStatus.READY,
            reason="crash checkpoint exists",
            checkpoint_id=f"checkpoint:{thread_id}",
        )


def _ready_task(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    queue = TaskQueue(store)
    task = queue.create(workspace_id="test", agent_id="agent")
    queue.transition(task.task_id, TaskState.READY)
    return store, queue, task.task_id


def _task_state(store: SQLiteStore, task_id: str) -> TaskState:
    with store.connection() as conn:
        row = conn.execute("SELECT state FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    return TaskState(row["state"])


@pytest.mark.parametrize(
    ("resume_token", "usable"),
    [
        pytest.param(None, False, id="none"),
        pytest.param("", False, id="empty"),
        pytest.param("   \t", False, id="whitespace"),
        pytest.param(b"resume", False, id="bytes"),
        pytest.param(7, False, id="integer"),
        pytest.param("thread-authority", True, id="valid-text"),
    ],
)
@pytest.mark.parametrize("allow_fresh_retry", [False, True])
def test_retry_dispatch_uses_one_usable_token_decision(
    tmp_path,
    resume_token: object,
    usable: bool,
    allow_fresh_retry: bool,
) -> None:
    store, queue, task_id = _ready_task(tmp_path)
    audit = AuditLog(store)
    runtime = _RetryDispatchRuntime(resume_token)
    policy = RetryPolicy(
        max_retries=1,
        retryable_error_codes=frozenset({RuntimeErrorCode.TRANSIENT}),
        allow_fresh_retry=allow_fresh_retry,
    )

    result = asyncio.run(
        TaskRuntimeCoordinator(queue, audit).start(
            runtime,
            RuntimeRequest(task_id, "thread-authority"),
            retry_policy=policy,
        )
    )

    if usable:
        assert result.outcome == RuntimeOutcome.COMPLETED
        assert runtime.run_calls == 1
        assert runtime.resume_calls == 1
        assert _task_state(store, task_id) == TaskState.COMPLETED
    elif allow_fresh_retry:
        assert result.outcome == RuntimeOutcome.COMPLETED
        assert runtime.run_calls == 2
        assert runtime.resume_calls == 0
        assert _task_state(store, task_id) == TaskState.COMPLETED
    else:
        assert result.outcome == RuntimeOutcome.FAILED
        assert runtime.run_calls == 1
        assert runtime.resume_calls == 0
        assert _task_state(store, task_id) == TaskState.FAILED

    events = audit.list_for(entity_type="task", entity_id=task_id)
    retry_started = [event for event in events if event.event_type == "runtime.retry_started"]
    expected_retry = usable or allow_fresh_retry
    assert bool(retry_started) is expected_retry
    if retry_started:
        assert retry_started[0].payload["resume"] is usable

    final = events[-1]
    assert final.event_type == "runtime.finished"
    assert final.payload["resume_token"] is None
    assert TaskRuntimeCoordinator(queue, audit).sessions.get(task_id) is None


def test_crash_left_retrying_session_is_recovered_by_resume_not_fresh_run(tmp_path) -> None:
    store, queue, task_id = _ready_task(tmp_path)
    queue.transition(task_id, TaskState.RUNNING)
    RuntimeSessionStore(store).record_active(
        task_id=task_id,
        runtime_id="retry-recovery-proof",
        thread_id="thread-retrying",
        resume_token="thread-retrying",
    )
    queue.transition(task_id, TaskState.RETRYING)

    runtime = _RecoveryRuntime()
    registry = RuntimeRegistry()
    registry.register(runtime)
    recovery = RuntimeRecoveryService(
        queue=queue,
        audit=AuditLog(store),
        runtimes=registry,
    )

    candidate = recovery.inspect()[0]
    assert candidate.disposition == RecoveryDisposition.AUTO_RESUME_CRASH
    assert candidate.task_state == TaskState.RETRYING

    execution = asyncio.run(recovery.resume_safe_crash_sessions(max_count=1))

    assert len(execution) == 1
    assert execution[0].succeeded
    assert runtime.run_calls == 0
    assert runtime.resume_calls == 1
    assert _task_state(store, task_id) == TaskState.COMPLETED


def test_restart_rejects_malformed_persisted_resume_token_before_runtime_effect(tmp_path) -> None:
    store, queue, task_id = _ready_task(tmp_path)
    queue.transition(task_id, TaskState.RUNNING)
    with store.connection() as conn:
        conn.execute(
            """
            INSERT INTO runtime_sessions(
                task_id, runtime_id, thread_id, resume_token, outcome, updated_at
            ) VALUES (?, ?, ?, ?, ?, datetime('now'))
            """,
            (
                task_id,
                "retry-recovery-proof",
                "thread-corrupt",
                b"corrupt-token",
                "__ACTIVE__",
            ),
        )

    runtime = _RecoveryRuntime()
    registry = RuntimeRegistry()
    registry.register(runtime)
    recovery = RuntimeRecoveryService(
        queue=queue,
        audit=AuditLog(store),
        runtimes=registry,
    )

    candidate = recovery.inspect()[0]
    assert candidate.disposition == RecoveryDisposition.INCONSISTENT_STATE
    execution = asyncio.run(recovery.resume_safe_crash_sessions(max_count=1))
    assert execution == ()
    assert runtime.run_calls == 0
    assert runtime.resume_calls == 0
    assert _task_state(store, task_id) == TaskState.RUNNING
