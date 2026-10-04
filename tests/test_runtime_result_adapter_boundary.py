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
    canonical_runtime_result,
)
from nika_core.runtime.coordinator import TaskRuntimeCoordinator
from nika_core.runtime.idempotency import IdempotencyLedger, IdempotencyStatus


_PRIVATE_MARKER = "SYNTHETIC_UNTRUSTED_ADAPTER_SECRET"


class _HostileResult:
    def __getattribute__(self, name: str):
        raise AssertionError(f"{_PRIVATE_MARKER}: accessed {name}")


class _ResultSubclass(RuntimeResult):
    pass


def _forged_result() -> RuntimeResult:
    result = RuntimeResult(outcome=RuntimeOutcome.COMPLETED)
    object.__setattr__(result, "outcome", "completed")
    return result


_BAD_RESULTS = (
    lambda: None,
    object,
    _HostileResult,
    _forged_result,
    lambda: _ResultSubclass(outcome=RuntimeOutcome.COMPLETED),
)


class _Adapter:
    runtime_id = "malformed-result-runtime"
    capabilities = frozenset({RuntimeCapability.DURABLE_RESUME})

    def __init__(self, result: object) -> None:
        self.result = result
        self.run_calls = 0
        self.resume_calls = 0

    async def run(self, request: RuntimeRequest):
        del request
        self.run_calls += 1
        return self.result

    async def resume(self, request: RuntimeResumeRequest):
        del request
        self.resume_calls += 1
        return self.result

    async def probe_resume(self, *, task_id: str, thread_id: str, resume_token: str):
        del task_id, thread_id, resume_token
        return RuntimeResumeProbe(
            status=RuntimeResumeProbeStatus.READY,
            reason="existing durable checkpoint",
            checkpoint_id="checkpoint:malformed-result-runtime",
        )


def _ready(tmp_path):
    store = SQLiteStore(tmp_path / "Українські дані" / "nika.db")
    store.initialize()
    queue = TaskQueue(store)
    task = queue.create(workspace_id="default", agent_id="nika.default")
    queue.transition(task.task_id, TaskState.READY)
    audit = AuditLog(store)
    return store, queue, task.task_id, audit, TaskRuntimeCoordinator(queue, audit)


@pytest.mark.parametrize("factory", _BAD_RESULTS)
def test_invalid_run_result_fails_closed_and_does_not_orphan_running_task(
    tmp_path, factory
):
    _, queue, task_id, audit, coordinator = _ready(tmp_path)
    runtime = _Adapter(factory())

    result = asyncio.run(coordinator.start(runtime, RuntimeRequest(task_id, "thread-1")))

    assert runtime.run_calls == 1
    assert result.outcome is RuntimeOutcome.FAILED
    assert result.error_code is RuntimeErrorCode.INTERNAL
    assert result.error == "runtime execution failed"
    assert queue.get(task_id).state is TaskState.FAILED
    assert coordinator.sessions.get(task_id) is None
    events = audit.list_for(entity_type="task", entity_id=task_id)
    assert events[-1].event_type == "runtime.finished"
    assert _PRIVATE_MARKER not in str(events)


@pytest.mark.parametrize("factory", _BAD_RESULTS)
def test_invalid_saved_resume_completes_claim_without_persisting_invalid_result(
    tmp_path, factory
):
    store, queue, task_id, audit, coordinator = _ready(tmp_path)
    queue.transition(task_id, TaskState.RUNNING)
    coordinator.sessions.record_active(
        task_id=task_id,
        runtime_id=_Adapter.runtime_id,
        thread_id="thread-1",
        resume_token="token-1",
    )
    runtime = _Adapter(factory())

    result = asyncio.run(coordinator.resume_saved(runtime, task_id=task_id))

    assert runtime.run_calls == 0
    assert runtime.resume_calls == 1
    assert result.outcome is RuntimeOutcome.FAILED
    assert result.error_code is RuntimeErrorCode.INTERNAL
    assert result.error == "runtime resume failed"
    assert queue.get(task_id).state is TaskState.FAILED
    assert coordinator.sessions.get(task_id) is None
    claims = [
        item for item in IdempotencyLedger(store).list_for_task(task_id)
        if item.operation_type == "runtime.recovery_resume"
    ]
    assert len(claims) == 1
    assert claims[0].status is IdempotencyStatus.COMPLETED
    assert _PRIVATE_MARKER not in str(claims)
    assert _PRIVATE_MARKER not in str(audit.list_for(entity_type="task", entity_id=task_id))


def test_valid_result_preserves_normal_coordinator_outcome_and_output(tmp_path):
    _, queue, task_id, _, coordinator = _ready(tmp_path)
    original = RuntimeResult(outcome=RuntimeOutcome.COMPLETED, output={"done": True})

    result = asyncio.run(
        coordinator.start(_Adapter(original), RuntimeRequest(task_id, "thread-1"))
    )

    assert result == original
    assert result.output == {"done": True}
    assert queue.get(task_id).state is TaskState.COMPLETED


def test_canonical_result_rejects_forgery_without_consulting_hostile_properties():
    for factory in _BAD_RESULTS:
        with pytest.raises((TypeError, ValueError)):
            canonical_runtime_result(factory())
