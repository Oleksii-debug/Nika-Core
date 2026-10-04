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
    RuntimeResumeMode,
    RuntimeResumeProbe,
    RuntimeResumeProbeStatus,
    RuntimeResumeRequest,
)
from nika_core.runtime.coordinator import TaskRuntimeCoordinator
from nika_core.runtime.idempotency import IdempotencyLedger, IdempotencyStatus

_SECRET = "SYNTHETIC_PROVIDER_TOKEN_AND_PRIVATE_WINDOWS_PATH"


class _UnprintableProviderError(Exception):
    def __str__(self) -> str:
        raise AssertionError("untrusted provider error text must never be evaluated")


class _FaultingRuntime:
    runtime_id = "private-error-runtime"
    capabilities = frozenset({RuntimeCapability.CANCELLATION})

    def __init__(self, error_type: type[Exception] = RuntimeError) -> None:
        self.failure = error_type(_SECRET)
        self.cancel_calls = 0

    async def run(self, request: RuntimeRequest):
        del request
        raise self.failure

    async def resume(self, request: RuntimeResumeRequest):
        del request
        raise self.failure

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        del task_id, thread_id
        self.cancel_calls += 1
        raise self.failure


def _ready(tmp_path):
    store = SQLiteStore(tmp_path / "Українські дані Nika" / "nika.db")
    store.initialize()
    queue = TaskQueue(store)
    task = queue.create(workspace_id="default", agent_id="nika.default")
    queue.transition(task.task_id, TaskState.READY)
    audit = AuditLog(store)
    return store, queue, task.task_id, audit, TaskRuntimeCoordinator(queue, audit)


@pytest.mark.parametrize("error_type", [OSError, RuntimeError, _UnprintableProviderError])
def test_runtime_start_never_projects_provider_exception_text(tmp_path, error_type):
    store, queue, task_id, audit, coordinator = _ready(tmp_path)
    runtime = _FaultingRuntime(error_type)

    result = asyncio.run(coordinator.start(runtime, RuntimeRequest(task_id, "private-thread")))

    assert result.outcome is RuntimeOutcome.FAILED
    assert result.error_code is RuntimeErrorCode.INTERNAL
    assert result.error == "runtime execution failed"
    assert queue.get(task_id).state is TaskState.FAILED
    assert _SECRET not in str(result)
    assert _SECRET not in str(audit.list_for(entity_type="task", entity_id=task_id))


@pytest.mark.parametrize("error_type", [OSError, RuntimeError, _UnprintableProviderError])
def test_runtime_resume_never_projects_provider_exception_text(tmp_path, error_type):
    _, _, task_id, _, coordinator = _ready(tmp_path)
    request = RuntimeResumeRequest(
        task_id=task_id,
        thread_id="private-thread",
        resume_token="private-token",
        mode=RuntimeResumeMode.CONTINUE,
    )

    result = asyncio.run(coordinator._safe_resume(_FaultingRuntime(error_type), request))

    assert result.outcome is RuntimeOutcome.FAILED
    assert result.error_code is RuntimeErrorCode.INTERNAL
    assert result.error == "runtime resume failed"
    assert _SECRET not in str(result)


@pytest.mark.parametrize("error_type", [OSError, RuntimeError, _UnprintableProviderError])
def test_cancel_uncertain_preserves_authority_without_audit_secret(tmp_path, error_type):
    store, queue, task_id, audit, coordinator = _ready(tmp_path)
    queue.transition(task_id, TaskState.RUNNING)
    coordinator.sessions.record_active(
        task_id=task_id,
        runtime_id=_FaultingRuntime.runtime_id,
        thread_id="private-thread",
        resume_token="private-token",
    )
    runtime = _FaultingRuntime(error_type)

    with pytest.raises(error_type) as caught:
        asyncio.run(coordinator.cancel(runtime, task_id=task_id, thread_id="private-thread"))
    assert caught.value is runtime.failure
    assert runtime.cancel_calls == 1
    assert queue.get(task_id).state is TaskState.RUNNING
    assert coordinator.sessions.get(task_id) is not None
    records = IdempotencyLedger(store).list_for_task(task_id)
    assert len(records) == 1
    assert records[0].operation_type == "runtime.cancel"
    assert records[0].status is IdempotencyStatus.UNCERTAIN
    events = audit.list_for(entity_type="task", entity_id=task_id)
    assert [event.event_type for event in events] == [
        "runtime.cancel_requested",
        "runtime.cancel_uncertain",
    ]
    assert events[-1].payload["error"] == error_type.__name__
    assert events[-1].payload["operation_key"] == records[0].operation_key
    assert _SECRET not in str(events)


class _CheckpointFaultingRuntime(_FaultingRuntime):
    capabilities = frozenset({
        RuntimeCapability.CANCELLATION,
        RuntimeCapability.DURABLE_RESUME,
    })

    async def probe_resume(self, *, task_id: str, thread_id: str, resume_token: str):
        del task_id, thread_id, resume_token
        return RuntimeResumeProbe(
            status=RuntimeResumeProbeStatus.READY,
            reason="known checkpoint",
            checkpoint_id="checkpoint:private-error-runtime",
        )


@pytest.mark.parametrize("error_type", [OSError, RuntimeError, _UnprintableProviderError])
def test_durable_resume_redacts_provider_exception_and_completes_recovery_claim(
    tmp_path, error_type
):
    store, queue, task_id, audit, coordinator = _ready(tmp_path)
    queue.transition(task_id, TaskState.RUNNING)
    coordinator.sessions.record_active(
        task_id=task_id,
        runtime_id=_CheckpointFaultingRuntime.runtime_id,
        thread_id="private-thread",
        resume_token="private-token",
    )
    runtime = _CheckpointFaultingRuntime(error_type)

    result = asyncio.run(coordinator.resume_saved(runtime, task_id=task_id))

    assert result.outcome is RuntimeOutcome.FAILED
    assert result.error_code is RuntimeErrorCode.INTERNAL
    assert result.error == "runtime resume failed"
    assert queue.get(task_id).state is TaskState.FAILED
    assert coordinator.sessions.get(task_id) is None
    claims = [
        record for record in IdempotencyLedger(store).list_for_task(task_id)
        if record.operation_type == "runtime.recovery_resume"
    ]
    assert len(claims) == 1
    assert claims[0].status is IdempotencyStatus.COMPLETED
    assert claims[0].result["checkpoint_id"] == "checkpoint:private-error-runtime"
    events = audit.list_for(entity_type="task", entity_id=task_id)
    assert "runtime.saved_resume_started" in [event.event_type for event in events]
    assert _SECRET not in str(result)
    assert _SECRET not in str(events)
    assert _SECRET not in str(claims)
