from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.runtime.contracts import (
    RuntimeOutcome,
    RuntimeResult,
    RuntimeResumeProbe,
    RuntimeResumeProbeStatus,
)
from nika_core.runtime.coordinator import TaskRuntimeCoordinator
from nika_core.runtime.idempotency import IdempotencyLedger
from nika_core.runtime.session_store import RuntimeSessionStore

_CANARY = "P10_CONTROL_QA_SYNTHETIC_RUNTIME_PROBE_SECRET_91d0e6"


class _RaisingProbeRuntime:
    runtime_id = "qa-probe-secret"
    capabilities = frozenset()

    def __init__(self) -> None:
        self.resume_calls = 0

    async def run(self, request):
        del request
        raise AssertionError("recovery must resume, not run")

    async def resume(self, request):
        del request
        self.resume_calls += 1
        return RuntimeResult(outcome=RuntimeOutcome.COMPLETED)

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        del task_id, thread_id
        return False

    async def probe_resume(self, *, task_id: str, thread_id: str, resume_token: str):
        del task_id, thread_id, resume_token
        raise RuntimeError(f"Authorization: Bearer {_CANARY}")


class _UnsafeReasonProbeRuntime(_RaisingProbeRuntime):
    async def probe_resume(self, *, task_id: str, thread_id: str, resume_token: str):
        del task_id, thread_id, resume_token
        return RuntimeResumeProbe(
            status=RuntimeResumeProbeStatus.UNREADABLE,
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
        runtime_id="qa-probe-secret",
        thread_id="qa-thread",
        resume_token="qa-thread",
    )
    return store, queue, task.task_id


def _assert_no_recovery_effect(store: SQLiteStore, task_id: str) -> None:
    records = IdempotencyLedger(store).list_for_task(task_id)
    assert all(record.operation_type != "runtime.recovery_resume" for record in records)


def test_probe_exception_secret_is_not_reexposed_by_coordinator(tmp_path: Path) -> None:
    async def scenario() -> None:
        store, queue, task_id = _active_runtime_task(tmp_path)
        runtime = _RaisingProbeRuntime()
        coordinator = TaskRuntimeCoordinator(queue, AuditLog(store), recovery_owner_id="qa-owner")

        with pytest.raises(ValueError) as exc_info:
            await coordinator.resume_saved(runtime, task_id=task_id)

        assert _CANARY not in str(exc_info.value)
        assert runtime.resume_calls == 0
        assert queue.get(task_id).state == TaskState.RUNNING
        _assert_no_recovery_effect(store, task_id)

    asyncio.run(scenario())


def test_probe_reason_secret_is_not_reexposed_by_coordinator(tmp_path: Path) -> None:
    async def scenario() -> None:
        store, queue, task_id = _active_runtime_task(tmp_path)
        runtime = _UnsafeReasonProbeRuntime()
        coordinator = TaskRuntimeCoordinator(queue, AuditLog(store), recovery_owner_id="qa-owner")

        with pytest.raises(ValueError) as exc_info:
            await coordinator.resume_saved(runtime, task_id=task_id)

        assert _CANARY not in str(exc_info.value)
        assert runtime.resume_calls == 0
        assert queue.get(task_id).state == TaskState.RUNNING
        _assert_no_recovery_effect(store, task_id)

    asyncio.run(scenario())
