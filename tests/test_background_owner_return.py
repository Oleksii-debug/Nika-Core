from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nika_core.background_life import BackgroundAction, BackgroundWorkKind, OwnerPresence
from nika_core.background_owner_return import (
    RunningBackgroundAction,
    WindowsBackgroundOwnerReturnController,
)
from nika_core.background_runtime import BackgroundDispatchGuard
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditIntegrityError, AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.resources.contracts import ResourceBudget, ResourceSnapshot
from nika_core.resources.manager import ResourceManager
from nika_core.runtime.contracts import (
    RuntimeCapability,
    RuntimeOutcome,
    RuntimeRequest,
    RuntimeResult,
    RuntimeResumeProbe,
    RuntimeResumeProbeStatus,
    RuntimeResumeRequest,
)
from nika_core.runtime.coordinator import TaskRuntimeCoordinator
from nika_core.windows_owner_presence import WindowsOwnerPresenceObserver


class FakeLastInputApi:
    def __init__(self, *, last_ticks: list[int], current_ticks: list[int]) -> None:
        self._last_ticks = list(last_ticks)
        self._current_ticks = list(current_ticks)

    def get_last_input_tick_ms(self) -> int:
        return self._last_ticks.pop(0)

    def get_tick_count64_ms(self) -> int:
        return self._current_ticks.pop(0)


class ExplodingLastInputApi:
    def get_last_input_tick_ms(self) -> int:
        raise OSError("synthetic presence failure")

    def get_tick_count64_ms(self) -> int:
        raise AssertionError("must not be called")


class StableResourceObserver:
    def snapshot(self) -> ResourceSnapshot:
        return ResourceSnapshot(
            cpu_percent=10.0,
            memory_percent=20.0,
            available_memory_bytes=2_000_000_000,
            power_plugged=True,
        )


class PausableRuntime:
    runtime_id = "pausable-runtime"
    capabilities = frozenset(
        {
            RuntimeCapability.DURABLE_RESUME,
            RuntimeCapability.CANCELLATION,
        }
    )

    def __init__(
        self,
        *,
        accepted: bool = True,
        probe_status: RuntimeResumeProbeStatus = RuntimeResumeProbeStatus.READY,
    ) -> None:
        self.accepted = accepted
        self.probe_status = probe_status
        self.probe_calls: list[tuple[str, str, str]] = []
        self.cancel_calls: list[tuple[str, str]] = []

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        raise AssertionError("run is not used by this controller test")

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        raise AssertionError("resume is not used by this controller test")

    async def probe_resume(
        self,
        *,
        task_id: str,
        thread_id: str,
        resume_token: str,
    ) -> RuntimeResumeProbe:
        self.probe_calls.append((task_id, thread_id, resume_token))
        return RuntimeResumeProbe(
            status=self.probe_status,
            reason="owner-return checkpoint proof",
            checkpoint_id=(
                f"checkpoint:{resume_token}"
                if self.probe_status is RuntimeResumeProbeStatus.READY
                else None
            ),
        )

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        self.cancel_calls.append((task_id, thread_id))
        return self.accepted


class CompletingResumableRuntime(PausableRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.resume_calls: list[RuntimeResumeRequest] = []
        self.probe_calls: list[tuple[str, str, str]] = []

    async def probe_resume(
        self,
        *,
        task_id: str,
        thread_id: str,
        resume_token: str,
    ) -> RuntimeResumeProbe:
        self.probe_calls.append((task_id, thread_id, resume_token))
        return RuntimeResumeProbe(
            status=RuntimeResumeProbeStatus.READY,
            reason="test checkpoint is durable",
            checkpoint_id=f"checkpoint:{resume_token}",
        )

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        self.resume_calls.append(request)
        return RuntimeResult(
            outcome=RuntimeOutcome.COMPLETED,
            output={"resumed_task_id": request.task_id},
        )


def _running_runtime_state(
    tmp_path: Path,
) -> tuple[TaskQueue, AuditLog, TaskRuntimeCoordinator, str, str]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    queue = TaskQueue(store)
    audit = AuditLog(store)
    coordinator = TaskRuntimeCoordinator(queue, audit, recovery_owner_id="owner-return-test")
    task = queue.create(workspace_id="living", agent_id="nika")
    queue.transition(task.task_id, TaskState.READY)
    queue.transition(task.task_id, TaskState.RUNNING)
    thread_id = f"background-{task.task_id}"
    coordinator.sessions.record_active(
        task_id=task.task_id,
        runtime_id=PausableRuntime.runtime_id,
        thread_id=thread_id,
        resume_token="resume-token",
    )
    audit.append(
        event_type="background.dispatch_permitted",
        entity_type="task",
        entity_id=task.task_id,
        payload={"work_kind": "reading_research", "resumed": False},
    )
    audit.append(
        event_type="runtime.started",
        entity_type="task",
        entity_id=task.task_id,
        payload={"runtime_id": PausableRuntime.runtime_id, "thread_id": thread_id},
    )
    return queue, audit, coordinator, task.task_id, thread_id


def _presence(
    audit: AuditLog,
    *,
    presence: OwnerPresence,
    observed_at: datetime,
) -> WindowsOwnerPresenceObserver:
    if presence is OwnerPresence.AWAY:
        api = FakeLastInputApi(
            last_ticks=[1_000, 1_000, 1_000, 1_000],
            current_ticks=[40_000, 100_000],
        )
    else:
        api = FakeLastInputApi(
            last_ticks=[99_500, 99_500],
            current_ticks=[100_000],
        )
    return WindowsOwnerPresenceObserver(
        audit,
        away_after_seconds=60,
        api=api,
        clock=lambda: observed_at,
    )


def test_fresh_away_leaves_running_background_task_untouched(tmp_path: Path) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    queue, audit, coordinator, task_id, thread_id = _running_runtime_state(tmp_path)
    runtime = PausableRuntime()
    presence = _presence(audit, presence=OwnerPresence.AWAY, observed_at=now)
    assert presence.observe().presence is OwnerPresence.ACTIVE
    controller = WindowsBackgroundOwnerReturnController(
        coordinator=coordinator,
        audit=audit,
        presence=presence,
        clock=lambda: now,
    )

    result = asyncio.run(
        controller.reconcile(runtime=runtime, task_id=task_id, thread_id=thread_id)
    )

    assert result.action is RunningBackgroundAction.CONTINUE
    assert result.reason == "owner_away"
    assert result.pause_applied is False
    assert runtime.probe_calls == []
    assert runtime.cancel_calls == []
    assert queue.get(task_id).state is TaskState.RUNNING
    assert coordinator.sessions.get(task_id).is_active is True


def test_active_owner_delegates_durable_pause_to_canonical_coordinator(tmp_path: Path) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    queue, audit, coordinator, task_id, thread_id = _running_runtime_state(tmp_path)
    runtime = PausableRuntime()
    controller = WindowsBackgroundOwnerReturnController(
        coordinator=coordinator,
        audit=audit,
        presence=_presence(audit, presence=OwnerPresence.ACTIVE, observed_at=now),
        clock=lambda: now,
    )

    result = asyncio.run(
        controller.reconcile(runtime=runtime, task_id=task_id, thread_id=thread_id)
    )

    assert result.action is RunningBackgroundAction.PAUSED
    assert result.reason == "owner_active"
    assert result.pause_applied is True
    assert runtime.probe_calls == [(task_id, thread_id, "resume-token")]
    assert runtime.cancel_calls == [(task_id, thread_id)]
    assert queue.get(task_id).state is TaskState.PAUSED
    session = coordinator.sessions.get(task_id)
    assert session is not None
    assert session.outcome.value == "paused"
    assert session.resume_token == "resume-token"
    pause_events = [
        event
        for event in audit.list_for(entity_type="task", entity_id=task_id)
        if event.event_type == "background.running_paused_for_owner"
    ]
    assert len(pause_events) == 1
    marker = pause_events[0]
    with queue.store.connection() as conn:
        row = conn.execute(
            "SELECT event_id, previous_state, new_state FROM task_events "
            "WHERE task_id = ? ORDER BY event_id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    assert row is not None
    assert marker.payload["task_event_id"] == int(row["event_id"])
    assert row["previous_state"] == TaskState.RUNNING.value
    assert row["new_state"] == TaskState.PAUSED.value
    assert type(marker.payload["pause_operation_key"]) is str


def test_owner_return_pause_round_trips_through_guarded_saved_resume(
    tmp_path: Path,
) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    queue, audit, coordinator, task_id, thread_id = _running_runtime_state(tmp_path)
    runtime = CompletingResumableRuntime()
    physical_presence = WindowsOwnerPresenceObserver(
        audit,
        away_after_seconds=60,
        api=FakeLastInputApi(
            last_ticks=[99_500] * 12,
            current_ticks=[100_000, 160_000, 160_001, 160_002, 160_003, 160_004],
        ),
        clock=lambda: now,
    )
    controller = WindowsBackgroundOwnerReturnController(
        coordinator=coordinator,
        audit=audit,
        presence=physical_presence,
        clock=lambda: now,
    )

    paused = asyncio.run(
        controller.reconcile(runtime=runtime, task_id=task_id, thread_id=thread_id)
    )

    assert paused.action is RunningBackgroundAction.PAUSED
    assert queue.get(task_id).state is TaskState.PAUSED

    resources = ResourceManager(queue.store, StableResourceObserver())
    resources.set_budget(
        ResourceBudget(
            scope="background_life",
            owner_id="living-agent",
            max_concurrent=1,
            max_cpu_percent=80.0,
            max_memory_percent=80.0,
        )
    )
    guard = BackgroundDispatchGuard(
        queue=queue,
        audit=audit,
        resources=resources,
        presence=physical_presence,
        source_id=physical_presence.source_id,
        max_presence_age_seconds=5.0,
        max_future_skew_seconds=1.0,
        clock=lambda: now,
    )

    async def resume_effect() -> object:
        return await coordinator.resume_saved(runtime, task_id=task_id)

    resumed = asyncio.run(
        guard.resume_paused(
            task_id=task_id,
            work_kind=BackgroundWorkKind.UNFINISHED_WORK,
            effect=resume_effect,
        )
    )

    assert resumed.action is BackgroundAction.RUN
    assert resumed.executed is True
    assert isinstance(resumed.effect_result, RuntimeResult)
    assert resumed.effect_result.outcome is RuntimeOutcome.COMPLETED
    assert queue.get(task_id).state is TaskState.COMPLETED
    assert runtime.probe_calls == [
        (task_id, thread_id, "resume-token"),
        (task_id, thread_id, "resume-token"),
    ]
    assert len(runtime.resume_calls) == 1
    assert runtime.resume_calls[0].task_id == task_id
    assert resources.active_count(scope="background_life", owner_id="living-agent") == 0
    event_types = [
        event.event_type for event in audit.list_for(entity_type="task", entity_id=task_id)
    ]
    assert "background.running_paused_for_owner" in event_types
    assert "background.resume_permitted" in event_types
    assert "runtime.saved_resume_started" in event_types
    assert "background.resume_returned" in event_types


def test_missing_checkpoint_blocks_owner_return_pause_before_cancel(tmp_path: Path) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    queue, audit, coordinator, task_id, thread_id = _running_runtime_state(tmp_path)
    runtime = PausableRuntime(probe_status=RuntimeResumeProbeStatus.MISSING)
    controller = WindowsBackgroundOwnerReturnController(
        coordinator=coordinator,
        audit=audit,
        presence=_presence(audit, presence=OwnerPresence.ACTIVE, observed_at=now),
        clock=lambda: now,
    )

    with pytest.raises(ValueError, match="checkpoint is not readable: missing"):
        asyncio.run(
            controller.reconcile(runtime=runtime, task_id=task_id, thread_id=thread_id)
        )

    assert runtime.probe_calls == [(task_id, thread_id, "resume-token")]
    assert runtime.cancel_calls == []
    assert queue.get(task_id).state is TaskState.RUNNING
    session = coordinator.sessions.get(task_id)
    assert session is not None
    assert session.is_active is True
    events = audit.list_for(entity_type="task", entity_id=task_id)
    assert not any(
        event.event_type == "background.running_paused_for_owner" for event in events
    )
    failures = [
        event for event in events if event.event_type == "background.running_pause_failed"
    ]
    assert len(failures) == 1
    assert failures[0].payload["reason"] == "owner_active"
    assert failures[0].payload["error_type"] == "ValueError"


def test_preexisting_paused_task_cannot_be_relabelled_owner_return(
    tmp_path: Path,
) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    queue, audit, coordinator, task_id, thread_id = _running_runtime_state(tmp_path)
    runtime = PausableRuntime()
    assert asyncio.run(
        coordinator.pause(runtime, task_id=task_id, thread_id=thread_id)
    ) is True
    assert queue.get(task_id).state is TaskState.PAUSED

    controller = WindowsBackgroundOwnerReturnController(
        coordinator=coordinator,
        audit=audit,
        presence=_presence(audit, presence=OwnerPresence.ACTIVE, observed_at=now),
        clock=lambda: now,
    )

    with pytest.raises(ValueError, match="requires a RUNNING task"):
        asyncio.run(
            controller.reconcile(runtime=runtime, task_id=task_id, thread_id=thread_id)
        )

    assert runtime.cancel_calls == [(task_id, thread_id)]
    assert not any(
        event.event_type == "background.running_paused_for_owner"
        for event in audit.list_for(entity_type="task", entity_id=task_id)
    )


def test_stale_presence_fails_closed_by_pausing_running_background_work(
    tmp_path: Path,
) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    queue, audit, coordinator, task_id, thread_id = _running_runtime_state(tmp_path)
    runtime = PausableRuntime()
    controller = WindowsBackgroundOwnerReturnController(
        coordinator=coordinator,
        audit=audit,
        presence=_presence(
            audit,
            presence=OwnerPresence.AWAY,
            observed_at=now - timedelta(seconds=30),
        ),
        max_presence_age_seconds=5,
        clock=lambda: now,
    )

    result = asyncio.run(
        controller.reconcile(runtime=runtime, task_id=task_id, thread_id=thread_id)
    )

    assert result.action is RunningBackgroundAction.PAUSED
    assert result.reason == "owner_presence_untrusted"
    assert queue.get(task_id).state is TaskState.PAUSED
    assert runtime.cancel_calls == [(task_id, thread_id)]


def test_physical_presence_api_failure_fails_closed_by_pausing(tmp_path: Path) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    queue, audit, coordinator, task_id, thread_id = _running_runtime_state(tmp_path)
    runtime = PausableRuntime()
    observer = WindowsOwnerPresenceObserver(
        audit,
        away_after_seconds=60,
        api=ExplodingLastInputApi(),
        clock=lambda: now,
    )
    controller = WindowsBackgroundOwnerReturnController(
        coordinator=coordinator,
        audit=audit,
        presence=observer,
        clock=lambda: now,
    )

    result = asyncio.run(
        controller.reconcile(runtime=runtime, task_id=task_id, thread_id=thread_id)
    )

    assert result.action is RunningBackgroundAction.PAUSED
    assert result.reason == "owner_presence_untrusted"
    assert queue.get(task_id).state is TaskState.PAUSED
    rejected = [
        event
        for event in audit.list_for(entity_type="task", entity_id=task_id)
        if event.event_type == "background.running_presence_rejected"
    ]
    assert rejected[-1].payload == {"error_type": "OSError"}


def test_task_advancing_before_pause_is_not_overwritten(tmp_path: Path) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    queue, audit, coordinator, task_id, thread_id = _running_runtime_state(tmp_path)
    queue.transition(task_id, TaskState.COMPLETED)
    runtime = PausableRuntime()
    controller = WindowsBackgroundOwnerReturnController(
        coordinator=coordinator,
        audit=audit,
        presence=_presence(audit, presence=OwnerPresence.ACTIVE, observed_at=now),
        clock=lambda: now,
    )

    with pytest.raises(ValueError, match="requires a RUNNING task"):
        asyncio.run(
            controller.reconcile(runtime=runtime, task_id=task_id, thread_id=thread_id)
        )

    assert queue.get(task_id).state is TaskState.COMPLETED
    assert runtime.cancel_calls == []


def test_wrong_thread_identity_does_not_reach_runtime_cancel(tmp_path: Path) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    queue, audit, coordinator, task_id, _thread_id = _running_runtime_state(tmp_path)
    runtime = PausableRuntime()
    controller = WindowsBackgroundOwnerReturnController(
        coordinator=coordinator,
        audit=audit,
        presence=_presence(audit, presence=OwnerPresence.ACTIVE, observed_at=now),
        clock=lambda: now,
    )

    with pytest.raises(ValueError, match="thread"):
        asyncio.run(
            controller.reconcile(
                runtime=runtime,
                task_id=task_id,
                thread_id="wrong-thread",
            )
        )

    assert queue.get(task_id).state is TaskState.RUNNING
    assert runtime.cancel_calls == []


def test_runtime_rejecting_pause_leaves_running_state_and_returns_not_active(
    tmp_path: Path,
) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    queue, audit, coordinator, task_id, thread_id = _running_runtime_state(tmp_path)
    runtime = PausableRuntime(accepted=False)
    controller = WindowsBackgroundOwnerReturnController(
        coordinator=coordinator,
        audit=audit,
        presence=_presence(audit, presence=OwnerPresence.ACTIVE, observed_at=now),
        clock=lambda: now,
    )

    result = asyncio.run(
        controller.reconcile(runtime=runtime, task_id=task_id, thread_id=thread_id)
    )

    assert result.action is RunningBackgroundAction.NOT_ACTIVE
    assert result.pause_applied is False
    assert queue.get(task_id).state is TaskState.RUNNING
    assert runtime.cancel_calls == [(task_id, thread_id)]


@pytest.mark.parametrize(
    ("max_age", "max_future", "error_type"),
    [
        (0, 0, ValueError),
        (61, 0, ValueError),
        (5, 6, ValueError),
        (True, 0, TypeError),
        (5, float("nan"), ValueError),
    ],
)
def test_presence_freshness_configuration_is_bounded(
    tmp_path: Path,
    max_age: object,
    max_future: object,
    error_type: type[Exception],
) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    _queue, audit, coordinator, _task_id, _thread_id = _running_runtime_state(tmp_path)

    with pytest.raises(error_type):
        WindowsBackgroundOwnerReturnController(
            coordinator=coordinator,
            audit=audit,
            presence=_presence(audit, presence=OwnerPresence.AWAY, observed_at=now),
            max_presence_age_seconds=max_age,  # type: ignore[arg-type]
            max_future_skew_seconds=max_future,  # type: ignore[arg-type]
            clock=lambda: now,
        )


def test_foreground_running_task_without_background_provenance_is_never_paused(
    tmp_path: Path,
) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    store = SQLiteStore(tmp_path / "foreground.db")
    store.initialize()
    queue = TaskQueue(store)
    audit = AuditLog(store)
    coordinator = TaskRuntimeCoordinator(queue, audit, recovery_owner_id="foreground-test")
    task = queue.create(workspace_id="default", agent_id="nika")
    queue.transition(task.task_id, TaskState.READY)
    queue.transition(task.task_id, TaskState.RUNNING)
    thread_id = f"foreground-{task.task_id}"
    coordinator.sessions.record_active(
        task_id=task.task_id,
        runtime_id=PausableRuntime.runtime_id,
        thread_id=thread_id,
        resume_token="foreground-token",
    )
    runtime = PausableRuntime()
    controller = WindowsBackgroundOwnerReturnController(
        coordinator=coordinator,
        audit=audit,
        presence=_presence(audit, presence=OwnerPresence.ACTIVE, observed_at=now),
        clock=lambda: now,
    )

    with pytest.raises(ValueError, match="background dispatch provenance"):
        asyncio.run(
            controller.reconcile(
                runtime=runtime,
                task_id=task.task_id,
                thread_id=thread_id,
            )
        )

    assert queue.get(task.task_id).state is TaskState.RUNNING
    assert runtime.cancel_calls == []
    assert audit.list_for(
        entity_type="owner_presence_source",
        entity_id="win32-owner-presence",
    ) == ()


def test_presence_from_different_audit_store_cannot_authorize_continue(
    tmp_path: Path,
) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    queue, audit, coordinator, task_id, _thread_id = _running_runtime_state(tmp_path)
    foreign_store = SQLiteStore(tmp_path / "foreign-presence.db")
    foreign_store.initialize()
    foreign_audit = AuditLog(foreign_store)
    foreign_presence = _presence(
        foreign_audit,
        presence=OwnerPresence.AWAY,
        observed_at=now,
    )
    runtime = PausableRuntime()
    with pytest.raises(ValueError, match="canonical AuditLog"):
        WindowsBackgroundOwnerReturnController(
            coordinator=coordinator,
            audit=audit,
            presence=foreign_presence,
            clock=lambda: now,
        )

    assert queue.get(task_id).state is TaskState.RUNNING
    assert runtime.cancel_calls == []


def test_runtime_start_before_background_permission_cannot_prove_background_origin(
    tmp_path: Path,
) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    store = SQLiteStore(tmp_path / "misordered-provenance.db")
    store.initialize()
    queue = TaskQueue(store)
    audit = AuditLog(store)
    coordinator = TaskRuntimeCoordinator(queue, audit, recovery_owner_id="misordered-test")
    task = queue.create(workspace_id="living", agent_id="nika")
    queue.transition(task.task_id, TaskState.READY)
    queue.transition(task.task_id, TaskState.RUNNING)
    thread_id = f"background-{task.task_id}"
    coordinator.sessions.record_active(
        task_id=task.task_id,
        runtime_id=PausableRuntime.runtime_id,
        thread_id=thread_id,
        resume_token="resume-token",
    )
    audit.append(
        event_type="runtime.started",
        entity_type="task",
        entity_id=task.task_id,
        payload={"runtime_id": PausableRuntime.runtime_id, "thread_id": thread_id},
    )
    audit.append(
        event_type="background.dispatch_permitted",
        entity_type="task",
        entity_id=task.task_id,
        payload={"work_kind": "reading_research", "resumed": False},
    )
    runtime = PausableRuntime()
    controller = WindowsBackgroundOwnerReturnController(
        coordinator=coordinator,
        audit=audit,
        presence=_presence(audit, presence=OwnerPresence.ACTIVE, observed_at=now),
        clock=lambda: now,
    )

    with pytest.raises(ValueError, match="matching runtime/thread start"):
        asyncio.run(
            controller.reconcile(
                runtime=runtime,
                task_id=task.task_id,
                thread_id=thread_id,
            )
        )

    assert queue.get(task.task_id).state is TaskState.RUNNING
    assert runtime.cancel_calls == []
    assert audit.list_for(
        entity_type="owner_presence_source",
        entity_id="win32-owner-presence",
    ) == ()


def test_foreign_runtime_start_after_permission_cannot_prove_background_origin(
    tmp_path: Path,
) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    store = SQLiteStore(tmp_path / "foreign-runtime-provenance.db")
    store.initialize()
    queue = TaskQueue(store)
    audit = AuditLog(store)
    coordinator = TaskRuntimeCoordinator(queue, audit, recovery_owner_id="foreign-runtime-test")
    task = queue.create(workspace_id="living", agent_id="nika")
    queue.transition(task.task_id, TaskState.READY)
    queue.transition(task.task_id, TaskState.RUNNING)
    thread_id = f"background-{task.task_id}"
    coordinator.sessions.record_active(
        task_id=task.task_id,
        runtime_id=PausableRuntime.runtime_id,
        thread_id=thread_id,
        resume_token="resume-token",
    )
    audit.append(
        event_type="background.dispatch_permitted",
        entity_type="task",
        entity_id=task.task_id,
        payload={"work_kind": "reading_research", "resumed": False},
    )
    audit.append(
        event_type="runtime.started",
        entity_type="task",
        entity_id=task.task_id,
        payload={"runtime_id": "other-runtime", "thread_id": thread_id},
    )
    runtime = PausableRuntime()
    controller = WindowsBackgroundOwnerReturnController(
        coordinator=coordinator,
        audit=audit,
        presence=_presence(audit, presence=OwnerPresence.ACTIVE, observed_at=now),
        clock=lambda: now,
    )

    with pytest.raises(ValueError, match="matching runtime/thread start"):
        asyncio.run(
            controller.reconcile(
                runtime=runtime,
                task_id=task.task_id,
                thread_id=thread_id,
            )
        )

    assert queue.get(task.task_id).state is TaskState.RUNNING
    assert runtime.cancel_calls == []


def test_controller_rejects_coordinator_from_different_audit_authority(
    tmp_path: Path,
) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    queue, audit, _coordinator, _task_id, _thread_id = _running_runtime_state(tmp_path)
    foreign_store = SQLiteStore(tmp_path / "foreign-coordinator.db")
    foreign_store.initialize()
    foreign_queue = TaskQueue(foreign_store)
    foreign_audit = AuditLog(foreign_store)
    foreign_coordinator = TaskRuntimeCoordinator(
        foreign_queue,
        foreign_audit,
        recovery_owner_id="foreign-coordinator",
    )

    with pytest.raises(ValueError, match="canonical AuditLog"):
        WindowsBackgroundOwnerReturnController(
            coordinator=foreign_coordinator,
            audit=audit,
            presence=_presence(audit, presence=OwnerPresence.ACTIVE, observed_at=now),
            clock=lambda: now,
        )

    assert audit.list_for(
        entity_type="owner_presence_source",
        entity_id="win32-owner-presence",
    ) == ()
    assert queue.count_ready == 0


def test_controller_rejects_queue_and_audit_from_different_stores(
    tmp_path: Path,
) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    store = SQLiteStore(tmp_path / "queue-store.db")
    store.initialize()
    queue = TaskQueue(store)
    audit_store = SQLiteStore(tmp_path / "audit-store.db")
    audit_store.initialize()
    audit = AuditLog(audit_store)
    coordinator = TaskRuntimeCoordinator(
        queue,
        audit,
        recovery_owner_id="split-authority",
    )

    with pytest.raises(ValueError, match="same SQLiteStore"):
        WindowsBackgroundOwnerReturnController(
            coordinator=coordinator,
            audit=audit,
            presence=_presence(audit, presence=OwnerPresence.ACTIVE, observed_at=now),
            clock=lambda: now,
        )

    assert audit.list_for(
        entity_type="owner_presence_source",
        entity_id="win32-owner-presence",
    ) == ()



def test_owner_return_marker_rejects_corrupt_pause_authority_json(
    tmp_path: Path,
) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    queue, audit, coordinator, task_id, thread_id = _running_runtime_state(tmp_path)
    runtime = PausableRuntime()
    controller = WindowsBackgroundOwnerReturnController(
        coordinator=coordinator,
        audit=audit,
        presence=_presence(audit, presence=OwnerPresence.ACTIVE, observed_at=now),
        clock=lambda: now,
    )
    task_event_fence, audit_event_fence = controller._capture_running_pause_fence(task_id)
    assert asyncio.run(
        coordinator.pause(runtime, task_id=task_id, thread_id=thread_id)
    ) is True

    with queue.store.connection() as conn:
        row = conn.execute(
            "SELECT event_id FROM audit_events "
            "WHERE entity_type = ? AND entity_id = ? "
            "AND event_type = ? ORDER BY event_id DESC LIMIT 1",
            ("task", task_id, "runtime.pause_confirmed"),
        ).fetchone()
        assert row is not None
        conn.execute(
            "UPDATE audit_events SET payload_json = ? WHERE event_id = ?",
            (
                '{"operation_key":"first","operation_key":"second"}',
                int(row["event_id"]),
            ),
        )

    with pytest.raises(AuditIntegrityError):
        controller._record_owner_return_pause(
            task_id=task_id,
            reason="owner_active",
            task_event_fence=task_event_fence,
            audit_event_fence=audit_event_fence,
        )

    with queue.store.connection() as conn:
        marker_count = conn.execute(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE entity_type = ? AND entity_id = ? AND event_type = ?",
            ("task", task_id, "background.running_paused_for_owner"),
        ).fetchone()[0]
    assert marker_count == 0


def test_owner_return_marker_rejects_pause_authority_mutation_after_strict_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    queue, audit, coordinator, task_id, thread_id = _running_runtime_state(tmp_path)
    runtime = PausableRuntime()
    controller = WindowsBackgroundOwnerReturnController(
        coordinator=coordinator,
        audit=audit,
        presence=_presence(audit, presence=OwnerPresence.ACTIVE, observed_at=now),
        clock=lambda: now,
    )
    task_event_fence, audit_event_fence = controller._capture_running_pause_fence(task_id)
    assert asyncio.run(
        coordinator.pause(runtime, task_id=task_id, thread_id=thread_id)
    ) is True

    original_list_for = audit.list_for
    mutated = False

    def racing_list_for(*, entity_type: str, entity_id: str):
        nonlocal mutated
        events = original_list_for(entity_type=entity_type, entity_id=entity_id)
        if entity_type == "task" and entity_id == task_id and not mutated:
            confirmed = next(
                event
                for event in events
                if event.event_type == "runtime.pause_confirmed"
                and event.event_id > audit_event_fence
            )
            changed = dict(confirmed.payload)
            changed["operation_key"] = "changed-after-strict-read"
            encoded = json.dumps(
                changed,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            with queue.store.connection() as conn:
                conn.execute(
                    "UPDATE audit_events SET payload_json = ? WHERE event_id = ?",
                    (encoded, confirmed.event_id),
                )
            mutated = True
        return events

    monkeypatch.setattr(audit, "list_for", racing_list_for)

    with pytest.raises(
        RuntimeError,
        match="authority changed during marker binding",
    ):
        controller._record_owner_return_pause(
            task_id=task_id,
            reason="owner_active",
            task_event_fence=task_event_fence,
            audit_event_fence=audit_event_fence,
        )

    with queue.store.connection() as conn:
        marker_count = conn.execute(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE entity_type = ? AND entity_id = ? AND event_type = ?",
            ("task", task_id, "background.running_paused_for_owner"),
        ).fetchone()[0]
    assert marker_count == 0
