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
from nika_core.runtime.idempotency import IdempotencyLedger, IdempotencyStatus
from nika_core.runtime.recovery import RecoveryDisposition, RuntimeRecoveryService
from nika_core.runtime.registry import RuntimeRegistry
from nika_core.runtime.session_store import RuntimeSessionStore


class RecoverableRuntime:
    runtime_id = "recoverable"
    capabilities = frozenset()

    async def run(self, request):
        raise AssertionError("startup recovery must call resume, not run")

    async def resume(self, request):
        return RuntimeResult(
            outcome=RuntimeOutcome.COMPLETED,
            output={"resumed": request.task_id},
        )

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        return False

    async def probe_resume(self, *, task_id: str, thread_id: str, resume_token: str):
        del task_id
        assert resume_token == thread_id
        return RuntimeResumeProbe(
            status=RuntimeResumeProbeStatus.READY,
            reason="test checkpoint exists",
            checkpoint_id=f"checkpoint:{thread_id}",
        )


class SessionMutatingProbeRuntime(RecoverableRuntime):
    runtime_id = "mutating-probe"

    def __init__(self, sessions: RuntimeSessionStore) -> None:
        self._sessions = sessions
        self.resume_calls = 0

    async def resume(self, request):
        self.resume_calls += 1
        raise AssertionError("changed recovery session must never be resumed")

    async def probe_resume(self, *, task_id: str, thread_id: str, resume_token: str):
        assert resume_token == thread_id
        self._sessions.delete(task_id)
        self._sessions.record_active(
            task_id=task_id,
            runtime_id=self.runtime_id,
            thread_id=f"{thread_id}-rebound",
            resume_token=f"{thread_id}-rebound",
        )
        return RuntimeResumeProbe(
            status=RuntimeResumeProbeStatus.READY,
            reason="old checkpoint was readable before session rebind",
            checkpoint_id=f"checkpoint:{thread_id}",
        )


def _services(tmp_path: Path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    queue = TaskQueue(store)
    audit = AuditLog(store)
    sessions = RuntimeSessionStore(store)
    ledger = IdempotencyLedger(store)
    runtimes = RuntimeRegistry()
    runtimes.register(RecoverableRuntime())
    recovery = RuntimeRecoveryService(
        queue=queue,
        audit=audit,
        runtimes=runtimes,
        sessions=sessions,
        idempotency=ledger,
    )
    return store, queue, audit, sessions, ledger, runtimes, recovery


def _running_task(queue: TaskQueue) -> str:
    task = queue.create(workspace_id="workspace", agent_id="agent")
    queue.transition(task.task_id, TaskState.READY)
    queue.transition(task.task_id, TaskState.RUNNING)
    return task.task_id


def test_startup_inventory_classifies_clean_crash_as_auto_resume(tmp_path: Path):
    _store, queue, _audit, sessions, _ledger, _runtimes, recovery = _services(tmp_path)
    task_id = _running_task(queue)
    sessions.record_active(
        task_id=task_id,
        runtime_id="recoverable",
        thread_id="thread-1",
        resume_token="thread-1",
    )

    candidates = recovery.inspect()

    assert len(candidates) == 1
    assert candidates[0].task_id == task_id
    assert candidates[0].disposition == RecoveryDisposition.AUTO_RESUME_CRASH
    assert candidates[0].task_state == TaskState.RUNNING
    assert candidates[0].unresolved_operation_keys == ()


def test_pending_external_side_effect_blocks_automatic_crash_resume(tmp_path: Path):
    _store, queue, _audit, sessions, ledger, _runtimes, recovery = _services(tmp_path)
    task_id = _running_task(queue)
    sessions.record_active(
        task_id=task_id,
        runtime_id="recoverable",
        thread_id="thread-side-effect",
        resume_token="thread-side-effect",
    )
    ledger.reserve(
        operation_key="send:message:42",
        task_id=task_id,
        operation_type="send_message",
        input_fingerprint="sha256:payload",
    )

    candidate = recovery.inspect()[0]

    assert candidate.disposition == RecoveryDisposition.RECONCILE_SIDE_EFFECTS
    assert candidate.unresolved_operation_keys == ("send:message:42",)
    assert ledger.require("send:message:42").status == IdempotencyStatus.UNCERTAIN

    repeated = recovery.inspect()[0]

    assert repeated.disposition == RecoveryDisposition.RECONCILE_SIDE_EFFECTS
    assert repeated.unresolved_operation_keys == ("send:message:42",)
    assert ledger.require("send:message:42").status == IdempotencyStatus.UNCERTAIN


def test_waiting_approval_is_never_auto_resumed(tmp_path: Path):
    _store, queue, _audit, sessions, _ledger, _runtimes, recovery = _services(tmp_path)
    task_id = _running_task(queue)
    queue.transition(task_id, TaskState.WAITING_APPROVAL)
    sessions.record_result(
        task_id=task_id,
        runtime_id="recoverable",
        thread_id="thread-approval",
        result=RuntimeResult(
            outcome=RuntimeOutcome.WAITING_APPROVAL,
            resume_token="approval-token",
        ),
    )

    candidate = recovery.inspect()[0]

    assert candidate.disposition == RecoveryDisposition.WAITING_APPROVAL


def test_missing_runtime_is_reported_instead_of_replayed(tmp_path: Path):
    _store, queue, _audit, sessions, _ledger, runtimes, _recovery = _services(tmp_path)
    task_id = _running_task(queue)
    sessions.record_active(
        task_id=task_id,
        runtime_id="missing-runtime",
        thread_id="thread-missing",
        resume_token="thread-missing",
    )
    recovery = RuntimeRecoveryService(
        queue=queue,
        audit=AuditLog(queue.store),
        runtimes=runtimes,
        sessions=sessions,
        idempotency=IdempotencyLedger(queue.store),
    )

    candidate = recovery.inspect()[0]

    assert candidate.disposition == RecoveryDisposition.MISSING_RUNTIME


def test_safe_startup_resume_recovers_only_clean_crash_sessions(tmp_path: Path):
    async def scenario() -> None:
        store, queue, audit, sessions, ledger, _runtimes, recovery = _services(tmp_path)
        safe_task_id = _running_task(queue)
        blocked_task_id = _running_task(queue)
        sessions.record_active(
            task_id=safe_task_id,
            runtime_id="recoverable",
            thread_id="thread-safe",
            resume_token="thread-safe",
        )
        sessions.record_active(
            task_id=blocked_task_id,
            runtime_id="recoverable",
            thread_id="thread-blocked",
            resume_token="thread-blocked",
        )
        ledger.reserve(
            operation_key="publish:item:99",
            task_id=blocked_task_id,
            operation_type="publish",
            input_fingerprint="sha256:item",
        )

        executions = await recovery.resume_safe_crash_sessions(max_count=8)

        assert len(executions) == 1
        assert executions[0].candidate.task_id == safe_task_id
        assert executions[0].succeeded
        with store.connection() as conn:
            safe_state = conn.execute(
                "SELECT state FROM tasks WHERE task_id = ?", (safe_task_id,)
            ).fetchone()["state"]
            blocked_state = conn.execute(
                "SELECT state FROM tasks WHERE task_id = ?", (blocked_task_id,)
            ).fetchone()["state"]
        assert safe_state == TaskState.COMPLETED.value
        assert blocked_state == TaskState.RUNNING.value
        assert sessions.get(safe_task_id) is None
        assert sessions.get(blocked_task_id) is not None
        event_types = [
            event.event_type
            for event in audit.list_for(entity_type="task", entity_id=safe_task_id)
        ]
        assert "runtime.recovery_auto_resume_requested" in event_types
        assert "runtime.crash_recovery_started" in event_types
        assert "runtime.finished" in event_types

    asyncio.run(scenario())


def test_zero_auto_resume_limit_fails_closed(tmp_path: Path):
    async def scenario() -> None:
        _store, _queue, _audit, _sessions, _ledger, _runtimes, recovery = _services(tmp_path)

        with pytest.raises(ValueError, match="max_count"):
            await recovery.resume_safe_crash_sessions(max_count=0)

    asyncio.run(scenario())


def test_checkpoint_preflight_fails_closed_if_session_changes_during_probe(tmp_path: Path):
    async def scenario() -> None:
        store = SQLiteStore(tmp_path / "nika-race.db")
        store.initialize()
        queue = TaskQueue(store)
        audit = AuditLog(store)
        sessions = RuntimeSessionStore(store)
        task_id = _running_task(queue)
        sessions.record_active(
            task_id=task_id,
            runtime_id="mutating-probe",
            thread_id="thread-original",
            resume_token="thread-original",
        )
        runtime = SessionMutatingProbeRuntime(sessions)
        runtimes = RuntimeRegistry()
        runtimes.register(runtime)
        recovery = RuntimeRecoveryService(
            queue=queue,
            audit=audit,
            runtimes=runtimes,
            sessions=sessions,
        )

        executions = await recovery.resume_safe_crash_sessions(max_count=1)

        assert len(executions) == 1
        assert executions[0].candidate.disposition == RecoveryDisposition.INCONSISTENT_STATE
        assert "changed during checkpoint preflight" in executions[0].candidate.reason
        assert runtime.resume_calls == 0
        rebound = sessions.get(task_id)
        assert rebound is not None
        assert rebound.thread_id == "thread-original-rebound"
        event_types = [
            event.event_type
            for event in audit.list_for(entity_type="task", entity_id=task_id)
        ]
        assert "runtime.recovery_checkpoint_blocked" in event_types
        assert "runtime.recovery_auto_resume_requested" not in event_types

    asyncio.run(scenario())
