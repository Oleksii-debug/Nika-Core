from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nika_core.background_life import OwnerPresence
from nika_core.background_owner_return import (
    RunningBackgroundAction,
    WindowsBackgroundOwnerReturnController,
)
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.runtime.contracts import (
    RuntimeCapability,
    RuntimeRequest,
    RuntimeResumeRequest,
    RuntimeResult,
)
from nika_core.runtime.coordinator import TaskRuntimeCoordinator
from nika_core.windows_owner_presence import WindowsOwnerPresenceObserver


class FakeLastInputApi:
    def __init__(self, *, last_ticks: list[int], current_tick: int) -> None:
        self._last_ticks = list(last_ticks)
        self._current_tick = current_tick

    def get_last_input_tick_ms(self) -> int:
        return self._last_ticks.pop(0)

    def get_tick_count64_ms(self) -> int:
        return self._current_tick


class ExplodingLastInputApi:
    def get_last_input_tick_ms(self) -> int:
        raise OSError("synthetic presence failure")

    def get_tick_count64_ms(self) -> int:
        raise AssertionError("must not be called")


class PausableRuntime:
    runtime_id = "pausable-runtime"
    capabilities = frozenset(
        {
            RuntimeCapability.DURABLE_RESUME,
            RuntimeCapability.CANCELLATION,
        }
    )

    def __init__(self, *, accepted: bool = True) -> None:
        self.accepted = accepted
        self.cancel_calls: list[tuple[str, str]] = []

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        raise AssertionError("run is not used by this controller test")

    async def resume(self, request: RuntimeResumeRequest) -> RuntimeResult:
        raise AssertionError("resume is not used by this controller test")

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        self.cancel_calls.append((task_id, thread_id))
        return self.accepted


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
    return queue, audit, coordinator, task.task_id, thread_id


def _presence(
    audit: AuditLog,
    *,
    presence: OwnerPresence,
    observed_at: datetime,
) -> WindowsOwnerPresenceObserver:
    if presence is OwnerPresence.AWAY:
        api = FakeLastInputApi(last_ticks=[1_000, 1_000], current_tick=100_000)
    else:
        api = FakeLastInputApi(last_ticks=[99_500, 99_500], current_tick=100_000)
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
    controller = WindowsBackgroundOwnerReturnController(
        coordinator=coordinator,
        audit=audit,
        presence=_presence(audit, presence=OwnerPresence.AWAY, observed_at=now),
        clock=lambda: now,
    )

    result = asyncio.run(
        controller.reconcile(runtime=runtime, task_id=task_id, thread_id=thread_id)
    )

    assert result.action is RunningBackgroundAction.CONTINUE
    assert result.reason == "owner_away"
    assert result.pause_applied is False
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
    assert runtime.cancel_calls == [(task_id, thread_id)]
    assert queue.get(task_id).state is TaskState.PAUSED
    session = coordinator.sessions.get(task_id)
    assert session is not None
    assert session.outcome.value == "paused"
    assert session.resume_token == "resume-token"


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

    with pytest.raises(ValueError, match="cannot be paused"):
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
