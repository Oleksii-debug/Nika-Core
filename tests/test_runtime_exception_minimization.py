from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.runtime import contracts as runtime_contracts
from nika_core.runtime import langgraph_runtime
from nika_core.runtime.coordinator import TaskRuntimeCoordinator
from nika_core.runtime.idempotency import IdempotencyLedger
from nika_core.runtime.session_store import RuntimeSessionStore

_CANARY = "P10_05_SYNTHETIC_BACKEND_SECRET_7f31c2"


class _FailingGraph:
    async def ainvoke(self, graph_input, *, config):  # type: ignore[no-untyped-def]
        del graph_input, config
        raise RuntimeError(f"Authorization: Bearer {_CANARY}")

    async def aget_state(self, config):  # type: ignore[no-untyped-def]
        del config
        raise RuntimeError(f"checkpoint backend token={_CANARY}")


class _RaisingCoordinatorProbeRuntime:
    runtime_id = "runtime-minimized-coordinator-probe"
    capabilities = frozenset()

    def __init__(self) -> None:
        self.resume_calls = 0

    async def run(self, request):  # type: ignore[no-untyped-def]
        del request
        raise AssertionError("durable recovery must resume, not run")

    async def resume(self, request):  # type: ignore[no-untyped-def]
        del request
        self.resume_calls += 1
        return runtime_contracts.RuntimeResult(outcome=runtime_contracts.RuntimeOutcome.COMPLETED)

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        del task_id, thread_id
        return False

    async def probe_resume(self, *, task_id: str, thread_id: str, resume_token: str):
        del task_id, thread_id, resume_token
        raise RuntimeError(f"Authorization: Bearer {_CANARY}")


class _UnsafeReasonCoordinatorProbeRuntime(_RaisingCoordinatorProbeRuntime):
    async def probe_resume(self, *, task_id: str, thread_id: str, resume_token: str):
        del task_id, thread_id, resume_token
        return runtime_contracts.RuntimeResumeProbe(
            status=runtime_contracts.RuntimeResumeProbeStatus.UNREADABLE,
            reason=f"checkpoint backend token={_CANARY}",
        )


def _active_runtime_task(tmp_path: Path) -> tuple[SQLiteStore, TaskQueue, str]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    queue = TaskQueue(store)
    task = queue.create(workspace_id="workspace", agent_id="agent")
    queue.transition(task.task_id, TaskState.READY)
    queue.transition(task.task_id, TaskState.RUNNING)
    RuntimeSessionStore(store).record_active(
        task_id=task.task_id,
        runtime_id="runtime-minimized-coordinator-probe",
        thread_id="runtime-minimized-coordinator-thread",
        resume_token="runtime-minimized-coordinator-thread",
    )
    return store, queue, task.task_id


def _assert_no_recovery_effect(store: SQLiteStore, task_id: str) -> None:
    records = IdempotencyLedger(store).list_for_task(task_id)
    assert all(record.operation_type != "runtime.recovery_resume" for record in records)


def test_framework_exception_is_minimized_at_runtime_result_boundary() -> None:
    runtime = langgraph_runtime.LangGraphRuntime(_FailingGraph())

    result = asyncio.run(
        runtime.run(
            runtime_contracts.RuntimeRequest(
                task_id="runtime-minimized-error-task",
                thread_id="runtime-minimized-error-thread",
                payload={"safe": "value"},
            )
        )
    )

    assert result.outcome is runtime_contracts.RuntimeOutcome.FAILED
    assert result.error_code is runtime_contracts.RuntimeErrorCode.INTERNAL
    assert result.error == "runtime execution failed"
    assert _CANARY not in (result.error or "")


def test_checkpoint_exception_is_minimized_at_resume_probe_boundary() -> None:
    runtime = langgraph_runtime.LangGraphRuntime(_FailingGraph())

    probe = asyncio.run(
        runtime.probe_resume(
            task_id="runtime-minimized-error-task",
            thread_id="runtime-minimized-error-thread",
            resume_token="runtime-minimized-error-thread",
        )
    )

    assert probe.status is runtime_contracts.RuntimeResumeProbeStatus.UNREADABLE
    assert probe.reason == "checkpoint lookup failed"
    assert _CANARY not in probe.reason


def test_coordinator_probe_exception_is_minimized_before_recovery_claim(tmp_path: Path) -> None:
    async def scenario() -> None:
        store, queue, task_id = _active_runtime_task(tmp_path)
        runtime = _RaisingCoordinatorProbeRuntime()
        coordinator = TaskRuntimeCoordinator(
            queue,
            AuditLog(store),
            recovery_owner_id="runtime-minimized-owner",
        )

        with pytest.raises(ValueError) as exc_info:
            await coordinator.resume_saved(runtime, task_id=task_id)

        assert str(exc_info.value) == "runtime resume checkpoint probe failed"
        assert _CANARY not in str(exc_info.value)
        assert exc_info.value.__cause__ is None
        assert runtime.resume_calls == 0
        assert queue.get(task_id).state == TaskState.RUNNING
        _assert_no_recovery_effect(store, task_id)

    asyncio.run(scenario())


def test_coordinator_probe_reason_is_minimized_before_recovery_claim(tmp_path: Path) -> None:
    async def scenario() -> None:
        store, queue, task_id = _active_runtime_task(tmp_path)
        runtime = _UnsafeReasonCoordinatorProbeRuntime()
        coordinator = TaskRuntimeCoordinator(
            queue,
            AuditLog(store),
            recovery_owner_id="runtime-minimized-owner",
        )

        with pytest.raises(ValueError) as exc_info:
            await coordinator.resume_saved(runtime, task_id=task_id)

        assert str(exc_info.value) == "runtime resume checkpoint is not readable: unreadable"
        assert _CANARY not in str(exc_info.value)
        assert runtime.resume_calls == 0
        assert queue.get(task_id).state == TaskState.RUNNING
        _assert_no_recovery_effect(store, task_id)

    asyncio.run(scenario())
