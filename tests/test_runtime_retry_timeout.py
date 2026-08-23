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
from nika_core.runtime.langgraph_runtime import LangGraphRuntime
from nika_core.runtime.retry import RetryPolicy


class _SlowGraph:
    async def ainvoke(self, graph_input, *, config):
        await asyncio.sleep(0.1)
        return {"done": True}


class _SlowCheckpointedGraph(_SlowGraph):
    async def aget_state(self, config):
        thread_id = config["configurable"]["thread_id"]
        return {
            "config": {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_id": "checkpoint-before-timeout",
                }
            }
        }


class _TransientDurableRuntime:
    runtime_id = "transient-proof"
    capabilities = frozenset({RuntimeCapability.DURABLE_RESUME})

    def __init__(self) -> None:
        self.run_calls = 0
        self.resume_calls = 0

    @staticmethod
    def initial_resume_token(*, task_id: str, thread_id: str) -> str:
        del task_id
        return thread_id

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        self.run_calls += 1
        return RuntimeResult(
            outcome=RuntimeOutcome.FAILED,
            error="temporary transport failure",
            error_code=RuntimeErrorCode.TRANSIENT,
            resume_token=request.thread_id,
        )

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        self.resume_calls += 1
        return RuntimeResult(outcome=RuntimeOutcome.COMPLETED, output={"resumed": True})

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        return False

    async def probe_resume(self, *, task_id: str, thread_id: str, resume_token: str):
        del task_id
        assert resume_token == thread_id
        return RuntimeResumeProbe(
            status=RuntimeResumeProbeStatus.READY,
            reason="retry checkpoint exists",
            checkpoint_id=f"checkpoint:{thread_id}",
        )


class _UnsafeTransientRuntime(_TransientDurableRuntime):
    runtime_id = "unsafe-transient-proof"

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        self.run_calls += 1
        return RuntimeResult(
            outcome=RuntimeOutcome.FAILED,
            error="temporary but no durable resume point",
            error_code=RuntimeErrorCode.TRANSIENT,
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


def test_runtime_request_rejects_non_positive_timeout() -> None:
    with pytest.raises(ValueError, match="timeout_seconds"):
        RuntimeRequest("task", "thread", timeout_seconds=0)


def test_langgraph_timeout_without_checkpoint_has_no_fake_resume_token() -> None:
    result = asyncio.run(
        LangGraphRuntime(_SlowGraph()).run(
            RuntimeRequest("task", "thread", timeout_seconds=0.01)
        )
    )

    assert result.outcome == RuntimeOutcome.FAILED
    assert result.error_code == RuntimeErrorCode.TIMEOUT
    assert result.resume_token is None
    assert "exceeded" in (result.error or "")


def test_langgraph_timeout_preserves_resume_token_only_after_checkpoint_proof() -> None:
    result = asyncio.run(
        LangGraphRuntime(_SlowCheckpointedGraph()).run(
            RuntimeRequest("task", "thread", timeout_seconds=0.01)
        )
    )

    assert result.outcome == RuntimeOutcome.FAILED
    assert result.error_code == RuntimeErrorCode.TIMEOUT
    assert result.resume_token == "thread"


def test_retry_policy_uses_durable_resume_and_audits_retry(tmp_path) -> None:
    store, queue, task_id = _ready_task(tmp_path)
    audit = AuditLog(store)
    coordinator = TaskRuntimeCoordinator(queue, audit)
    runtime = _TransientDurableRuntime()
    policy = RetryPolicy(
        max_retries=2,
        retryable_error_codes=frozenset({RuntimeErrorCode.TRANSIENT}),
    )

    result = asyncio.run(
        coordinator.start(
            runtime,
            RuntimeRequest(task_id, "thread-retry"),
            retry_policy=policy,
        )
    )

    assert result.outcome == RuntimeOutcome.COMPLETED
    assert runtime.run_calls == 1
    assert runtime.resume_calls == 1
    assert _task_state(store, task_id) == TaskState.COMPLETED
    events = audit.list_for(entity_type="task", entity_id=task_id)
    assert [event.event_type for event in events] == [
        "runtime.started",
        "runtime.session_bound",
        "runtime.retry_scheduled",
        "runtime.retry_started",
        "runtime.finished",
    ]
    assert events[2].payload["error_code"] == RuntimeErrorCode.TRANSIENT.value
    assert events[3].payload["resume"] is True


def test_retry_policy_fails_closed_without_resume_token(tmp_path) -> None:
    store, queue, task_id = _ready_task(tmp_path)
    runtime = _UnsafeTransientRuntime()
    policy = RetryPolicy(
        max_retries=3,
        retryable_error_codes=frozenset({RuntimeErrorCode.TRANSIENT}),
    )

    result = asyncio.run(
        TaskRuntimeCoordinator(queue, AuditLog(store)).start(
            runtime,
            RuntimeRequest(task_id, "thread-unsafe"),
            retry_policy=policy,
        )
    )

    assert result.outcome == RuntimeOutcome.FAILED
    assert runtime.run_calls == 1
    assert runtime.resume_calls == 0
    assert _task_state(store, task_id) == TaskState.FAILED


def test_retry_policy_backoff_is_bounded() -> None:
    policy = RetryPolicy(max_retries=4, base_delay_seconds=0.5, max_delay_seconds=1.0)
    assert policy.delay_seconds(retry_number=1) == 0.5
    assert policy.delay_seconds(retry_number=2) == 1.0
    assert policy.delay_seconds(retry_number=3) == 1.0
