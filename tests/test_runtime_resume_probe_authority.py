from __future__ import annotations

import asyncio

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
    canonical_resume_probe,
)
from nika_core.runtime.coordinator import TaskRuntimeCoordinator
from nika_core.runtime.idempotency import IdempotencyLedger
from nika_core.runtime.recovery import RecoveryDisposition, RuntimeRecoveryService
from nika_core.runtime.registry import RuntimeRegistry
from nika_core.runtime.session_store import RuntimeSessionStore


class _TextSubclass(str):
    pass


class _DuckProbe:
    can_resume = True
    status = RuntimeResumeProbeStatus.READY
    reason = "duck"
    checkpoint_id = "checkpoint:duck"


class _ProbeRuntime:
    runtime_id = "probe-authority"
    capabilities = frozenset()

    def __init__(self, probe: object) -> None:
        self.probe = probe
        self.resume_calls = 0

    async def run(self, request):
        del request
        raise AssertionError("recovery must resume, not run")

    async def resume(self, request):
        self.resume_calls += 1
        return RuntimeResult(
            outcome=RuntimeOutcome.COMPLETED,
            output={"task_id": request.task_id},
        )

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        del task_id, thread_id
        return False

    async def probe_resume(self, *, task_id: str, thread_id: str, resume_token: str):
        del task_id, thread_id, resume_token
        return self.probe


def _forged_probe(
    *,
    status: object = RuntimeResumeProbeStatus.READY,
    reason: object = "forged",
    checkpoint_id: object = "checkpoint:forged",
) -> RuntimeResumeProbe:
    probe = object.__new__(RuntimeResumeProbe)
    object.__setattr__(probe, "status", status)
    object.__setattr__(probe, "reason", reason)
    object.__setattr__(probe, "checkpoint_id", checkpoint_id)
    return probe


def _active_session(tmp_path, *, runtime_id: str = "probe-authority"):
    store = SQLiteStore(tmp_path / "probe-authority.db")
    store.initialize()
    queue = TaskQueue(store)
    task = queue.create(workspace_id="probe-authority", agent_id="worker")
    queue.transition(task.task_id, TaskState.READY)
    queue.transition(task.task_id, TaskState.RUNNING)
    sessions = RuntimeSessionStore(store)
    sessions.record_active(
        task_id=task.task_id,
        runtime_id=runtime_id,
        thread_id="thread-1",
        resume_token="resume-1",
    )
    return store, queue, sessions, task.task_id


def test_resume_probe_constructor_requires_exact_status_and_checkpoint_carriers() -> None:
    with pytest.raises(TypeError, match="exact RuntimeResumeProbeStatus"):
        RuntimeResumeProbe(
            status="ready",  # type: ignore[arg-type]
            reason="raw status must not become authority",
            checkpoint_id="checkpoint:1",
        )

    with pytest.raises(TypeError, match="exact string"):
        RuntimeResumeProbe(
            status=RuntimeResumeProbeStatus.READY,
            reason="hostile checkpoint must not become authority",
            checkpoint_id=_TextSubclass("checkpoint:1"),
        )

    with pytest.raises(ValueError, match="too long"):
        RuntimeResumeProbe(
            status=RuntimeResumeProbeStatus.READY,
            reason="bounded durable checkpoint authority",
            checkpoint_id="x" * 1025,
        )


@pytest.mark.parametrize(
    "probe",
    (
        _DuckProbe(),
        _forged_probe(status="ready"),
        _forged_probe(checkpoint_id=_TextSubclass("checkpoint:spoof")),
        _forged_probe(reason=_TextSubclass("forged reason")),
    ),
)
def test_canonical_probe_snapshot_rejects_duck_or_constructor_bypassed_evidence(
    probe: object,
) -> None:
    with pytest.raises((TypeError, ValueError)):
        canonical_resume_probe(probe)


@pytest.mark.parametrize(
    "probe",
    (
        _DuckProbe(),
        _forged_probe(status="ready"),
        _forged_probe(checkpoint_id=_TextSubclass("checkpoint:spoof")),
        _forged_probe(checkpoint_id="x" * 1025),
    ),
)
def test_direct_resume_rejects_uncanonical_probe_before_claim_or_runtime_effect(
    tmp_path,
    probe: object,
) -> None:
    store, queue, sessions, task_id = _active_session(tmp_path)
    runtime = _ProbeRuntime(probe)
    coordinator = TaskRuntimeCoordinator(
        queue,
        AuditLog(store),
        session_store=sessions,
        recovery_owner_id="probe-test",
    )

    with pytest.raises(ValueError, match="checkpoint probe failed"):
        asyncio.run(coordinator.resume_saved(runtime, task_id=task_id))

    assert runtime.resume_calls == 0
    assert [
        item
        for item in IdempotencyLedger(store).list_for_task(task_id)
        if item.operation_type == "runtime.recovery_resume"
    ] == []


@pytest.mark.parametrize(
    "probe",
    (
        _DuckProbe(),
        _forged_probe(status="ready"),
        _forged_probe(checkpoint_id=_TextSubclass("checkpoint:spoof")),
        _forged_probe(checkpoint_id="x" * 1025),
    ),
)
def test_startup_recovery_rejects_uncanonical_probe_without_resume_or_checkpoint_persistence(
    tmp_path,
    probe: object,
) -> None:
    store, queue, sessions, task_id = _active_session(tmp_path)
    runtime = _ProbeRuntime(probe)
    runtimes = RuntimeRegistry()
    runtimes.register(runtime)
    audit = AuditLog(store)
    recovery = RuntimeRecoveryService(
        queue=queue,
        audit=audit,
        runtimes=runtimes,
        sessions=sessions,
    )

    executions = asyncio.run(recovery.resume_safe_crash_sessions(max_count=1))

    assert len(executions) == 1
    assert executions[0].candidate.disposition is RecoveryDisposition.CHECKPOINT_UNAVAILABLE
    assert runtime.resume_calls == 0
    checkpoint_events = [
        event
        for event in audit.list_for(entity_type="task", entity_id=task_id)
        if event.event_type == "runtime.recovery_checkpoint_blocked"
    ]
    assert len(checkpoint_events) == 1
    assert "checkpoint:spoof" not in str(checkpoint_events[0].payload)
    assert "x" * 128 not in str(checkpoint_events[0].payload)
