from __future__ import annotations

import asyncio

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
from nika_core.runtime.idempotency import (
    IdempotencyConflictError,
    IdempotencyLedger,
    IdempotencyStatus,
)
from nika_core.runtime.recovery import RecoveryDisposition, RuntimeRecoveryService
from nika_core.runtime.registry import RuntimeRegistry


class _SimulatedProcessLoss(BaseException):
    """Bypass normal Exception handling like abrupt process loss."""


class _CancelRuntime:
    runtime_id = "cancel-proof"
    capabilities = frozenset(
        {
            RuntimeCapability.DURABLE_RESUME,
            RuntimeCapability.CANCELLATION,
        }
    )

    def __init__(self, *, cancel_results: list[bool | BaseException] | None = None) -> None:
        self.cancel_results = list(cancel_results or [True])
        self.cancel_calls = 0

    @staticmethod
    def initial_resume_token(*, task_id: str, thread_id: str) -> str:
        del task_id
        return thread_id

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        return RuntimeResult(outcome=RuntimeOutcome.COMPLETED)

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        return RuntimeResult(outcome=RuntimeOutcome.COMPLETED)

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        del task_id, thread_id
        self.cancel_calls += 1
        if not self.cancel_results:
            raise AssertionError("unexpected extra cancel call")
        result = self.cancel_results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result


class _RacingRuntime(_CancelRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        self.started.set()
        await self.release.wait()
        return RuntimeResult(
            outcome=RuntimeOutcome.COMPLETED,
            output={"runtime": "completed-after-cancel"},
        )


class _FaultingLedger(IdempotencyLedger):
    def complete_with_connection(self, conn, operation_key, result=None):
        del conn, operation_key, result
        raise RuntimeError("simulated local finalization failure")


def _ready(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    queue = TaskQueue(store)
    task = queue.create(workspace_id="proof", agent_id="agent")
    queue.transition(task.task_id, TaskState.READY)
    return store, queue, task.task_id


def _active(tmp_path, *, runtime: _CancelRuntime | None = None):
    store, queue, task_id = _ready(tmp_path)
    runtime = runtime or _CancelRuntime()
    queue.transition(task_id, TaskState.RUNNING)
    coordinator = TaskRuntimeCoordinator(queue, AuditLog(store))
    coordinator.sessions.record_active(
        task_id=task_id,
        runtime_id=runtime.runtime_id,
        thread_id="thread-cancel",
        resume_token="thread-cancel",
    )
    return store, queue, coordinator, runtime, task_id


def _state(store: SQLiteStore, task_id: str) -> TaskState:
    with store.connection() as conn:
        row = conn.execute("SELECT state FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    return TaskState(row["state"])


def _cancel_records(store: SQLiteStore, task_id: str):
    return tuple(
        record
        for record in IdempotencyLedger(store).list_for_task(task_id)
        if record.operation_type == "runtime.cancel"
    )


def test_accepted_cancel_commits_terminal_state_session_cleanup_and_dedup(tmp_path) -> None:
    store, _, coordinator, runtime, task_id = _active(tmp_path)

    assert asyncio.run(
        coordinator.cancel(runtime, task_id=task_id, thread_id="thread-cancel")
    ) is True

    assert _state(store, task_id) == TaskState.CANCELLED
    assert coordinator.sessions.get(task_id) is None
    records = _cancel_records(store, task_id)
    assert len(records) == 1
    assert records[0].status == IdempotencyStatus.COMPLETED
    assert records[0].result == {
        "accepted": True,
        "task_state": TaskState.CANCELLED.value,
    }
    assert runtime.cancel_calls == 1

    assert asyncio.run(
        coordinator.cancel(runtime, task_id=task_id, thread_id="thread-cancel")
    ) is True
    assert runtime.cancel_calls == 1

    event_types = [
        event.event_type
        for event in AuditLog(store).list_for(entity_type="task", entity_id=task_id)
    ]
    assert event_types[-2:] == ["runtime.cancel_requested", "runtime.cancel_accepted"]


def test_not_active_cancel_releases_reservation_and_allows_later_explicit_retry(tmp_path) -> None:
    runtime = _CancelRuntime(cancel_results=[False, True])
    store, _, coordinator, runtime, task_id = _active(tmp_path, runtime=runtime)

    assert asyncio.run(
        coordinator.cancel(runtime, task_id=task_id, thread_id="thread-cancel")
    ) is False
    assert _state(store, task_id) == TaskState.RUNNING
    assert coordinator.sessions.get(task_id) is not None
    assert _cancel_records(store, task_id) == ()

    assert asyncio.run(
        coordinator.cancel(runtime, task_id=task_id, thread_id="thread-cancel")
    ) is True
    assert runtime.cancel_calls == 2
    assert _state(store, task_id) == TaskState.CANCELLED


def test_cancel_exception_becomes_uncertain_and_blocks_startup_auto_resume(tmp_path) -> None:
    runtime = _CancelRuntime(cancel_results=[RuntimeError("transport lost after cancel")])
    store, queue, coordinator, runtime, task_id = _active(tmp_path, runtime=runtime)

    with pytest.raises(RuntimeError, match="transport lost"):
        asyncio.run(coordinator.cancel(runtime, task_id=task_id, thread_id="thread-cancel"))

    records = _cancel_records(store, task_id)
    assert len(records) == 1
    assert records[0].status == IdempotencyStatus.UNCERTAIN
    assert _state(store, task_id) == TaskState.RUNNING
    assert coordinator.sessions.get(task_id) is not None

    registry = RuntimeRegistry()
    registry.register(runtime)
    candidate = RuntimeRecoveryService(
        queue=TaskQueue(store),
        audit=AuditLog(store),
        runtimes=registry,
    ).inspect()[0]
    assert candidate.disposition == RecoveryDisposition.RECONCILE_SIDE_EFFECTS
    assert candidate.unresolved_operation_keys == (records[0].operation_key,)


def test_process_loss_after_durable_cancel_intent_leaves_pending_recovery_block(tmp_path) -> None:
    runtime = _CancelRuntime(cancel_results=[_SimulatedProcessLoss()])
    store, _, coordinator, runtime, task_id = _active(tmp_path, runtime=runtime)

    with pytest.raises(_SimulatedProcessLoss):
        asyncio.run(coordinator.cancel(runtime, task_id=task_id, thread_id="thread-cancel"))

    records = _cancel_records(store, task_id)
    assert len(records) == 1
    assert records[0].status == IdempotencyStatus.PENDING

    registry = RuntimeRegistry()
    registry.register(runtime)
    candidate = RuntimeRecoveryService(
        queue=TaskQueue(store),
        audit=AuditLog(store),
        runtimes=registry,
    ).inspect()[0]
    assert candidate.disposition == RecoveryDisposition.RECONCILE_SIDE_EFFECTS
    assert candidate.unresolved_operation_keys == (records[0].operation_key,)


def test_pending_or_uncertain_cancel_is_never_replayed_automatically(tmp_path) -> None:
    runtime = _CancelRuntime(cancel_results=[_SimulatedProcessLoss(), True])
    store, _, coordinator, runtime, task_id = _active(tmp_path, runtime=runtime)

    with pytest.raises(_SimulatedProcessLoss):
        asyncio.run(coordinator.cancel(runtime, task_id=task_id, thread_id="thread-cancel"))

    with pytest.raises(IdempotencyConflictError, match="pending or uncertain"):
        asyncio.run(coordinator.cancel(runtime, task_id=task_id, thread_id="thread-cancel"))
    assert runtime.cancel_calls == 1
    assert _state(store, task_id) == TaskState.RUNNING


def test_cancel_wins_race_against_late_runtime_completion(tmp_path) -> None:
    async def scenario() -> None:
        store, queue, task_id = _ready(tmp_path)
        audit = AuditLog(store)
        runtime = _RacingRuntime()
        coordinator = TaskRuntimeCoordinator(queue, audit)

        running = asyncio.create_task(
            coordinator.start(runtime, RuntimeRequest(task_id, "race-thread"))
        )
        await runtime.started.wait()
        assert _state(store, task_id) == TaskState.RUNNING
        assert coordinator.sessions.get(task_id) is not None

        assert await coordinator.cancel(
            runtime,
            task_id=task_id,
            thread_id="race-thread",
        ) is True
        assert _state(store, task_id) == TaskState.CANCELLED

        runtime.release.set()
        result = await running
        assert result.outcome == RuntimeOutcome.CANCELLED
        assert _state(store, task_id) == TaskState.CANCELLED
        assert coordinator.sessions.get(task_id) is None

        events = audit.list_for(entity_type="task", entity_id=task_id)
        assert "runtime.finished_after_cancel" in [event.event_type for event in events]
        assert events[-1].event_type == "runtime.finished"
        assert events[-1].payload["outcome"] == RuntimeOutcome.CANCELLED.value
        assert events[-1].payload["runtime_reported_outcome"] == RuntimeOutcome.COMPLETED.value

    asyncio.run(scenario())


def test_local_cancel_finalization_failure_rolls_back_without_partial_terminal_state(tmp_path) -> None:
    runtime = _CancelRuntime()
    store, queue, _, runtime, task_id = _active(tmp_path, runtime=runtime)
    coordinator = TaskRuntimeCoordinator(
        queue,
        AuditLog(store),
        idempotency=_FaultingLedger(store),
    )

    with pytest.raises(RuntimeError, match="local finalization failure"):
        asyncio.run(coordinator.cancel(runtime, task_id=task_id, thread_id="thread-cancel"))

    assert runtime.cancel_calls == 1
    assert _state(store, task_id) == TaskState.RUNNING
    assert coordinator.sessions.get(task_id) is not None
    records = _cancel_records(store, task_id)
    assert len(records) == 1
    assert records[0].status == IdempotencyStatus.PENDING
