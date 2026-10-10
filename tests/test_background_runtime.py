from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from pathlib import Path

import pytest

from nika_core.background_life import BackgroundAction, BackgroundWorkKind, OwnerPresence
from nika_core.background_runtime import BackgroundDispatchGuard, OwnerPresenceObservation
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.resources import ResourceBudget, ResourceManager, ResourceSnapshot
from nika_core.runtime.contracts import RuntimeOutcome, RuntimeRequest, RuntimeResult
from nika_core.runtime.coordinator import TaskRuntimeCoordinator
from nika_core.runtime.idempotency import (
    IdempotencyConflictError,
    IdempotencyLedger,
    IdempotencyStatus,
)


class _BehavioralTimezone(tzinfo):
    def __init__(self) -> None:
        self.calls = 0

    def utcoffset(self, _dt):
        self.calls += 1
        raise AssertionError("behavioral tzinfo must not execute")

    def dst(self, _dt):
        self.calls += 1
        raise AssertionError("behavioral tzinfo must not execute")


class SequencePresence:
    def __init__(self, observations: list[OwnerPresenceObservation]) -> None:
        self._observations = iter(observations)

    def observe(self) -> OwnerPresenceObservation:
        return next(self._observations)


class SequenceResourceObserver:
    def __init__(
        self,
        cpu_values: list[float] | None = None,
        power_values: list[bool | None] | None = None,
    ) -> None:
        self._cpu_values = iter(cpu_values or [10.0] * 20)
        self._power_values = iter(power_values or [True] * 20)

    def snapshot(self) -> ResourceSnapshot:
        return ResourceSnapshot(
            cpu_percent=next(self._cpu_values),
            memory_percent=20.0,
            available_memory_bytes=2_000_000_000,
            power_plugged=next(self._power_values),
        )


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    return store


def _ready_task(queue: TaskQueue) -> str:
    task = queue.create(workspace_id="living", agent_id="nika")
    queue.transition(task.task_id, TaskState.READY)
    return task.task_id


def _owner_return_paused_task(queue: TaskQueue, audit: AuditLog) -> tuple[str, int]:
    task_id = _ready_task(queue)
    queue.transition(task_id, TaskState.RUNNING)
    queue.transition(task_id, TaskState.PAUSED)
    with queue.store.connection() as conn:
        row = conn.execute(
            "SELECT event_id, new_state FROM task_events "
            "WHERE task_id = ? ORDER BY event_id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    assert row is not None
    assert row["new_state"] == TaskState.PAUSED.value
    pause_event_id = int(row["event_id"])
    audit.append(
        event_type="background.running_paused_for_owner",
        entity_type="task",
        entity_id=task_id,
        payload={
            "reason": "owner_active",
            "source_id": "win32-owner-presence",
            "task_event_id": pause_event_id,
        },
    )
    return task_id, pause_event_id


def _obs(
    sequence: int,
    presence: OwnerPresence,
    *,
    now: datetime,
    source_id: str = "win32-owner-presence",
    age_seconds: float = 0.0,
) -> OwnerPresenceObservation:
    return OwnerPresenceObservation(
        source_id=source_id,
        sequence=sequence,
        presence=presence,
        observed_at=now - timedelta(seconds=age_seconds),
    )


def _guard(
    *,
    store: SQLiteStore,
    observations: list[OwnerPresenceObservation],
    now: datetime,
    resource_observer: SequenceResourceObserver | None = None,
) -> tuple[BackgroundDispatchGuard, TaskQueue, AuditLog, ResourceManager]:
    queue = TaskQueue(store)
    audit = AuditLog(store)
    resources = ResourceManager(store, resource_observer or SequenceResourceObserver())
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
        presence=SequencePresence(observations),
        source_id="win32-owner-presence",
        max_presence_age_seconds=5.0,
        max_future_skew_seconds=1.0,
        clock=lambda: now,
    )
    return guard, queue, audit, resources


def test_fresh_away_evidence_dispatches_exactly_once(tmp_path: Path) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    store = _store(tmp_path)
    guard, queue, audit, resources = _guard(
        store=store,
        observations=[
            _obs(1, OwnerPresence.AWAY, now=now),
            _obs(2, OwnerPresence.AWAY, now=now),
            _obs(3, OwnerPresence.AWAY, now=now),
            _obs(4, OwnerPresence.AWAY, now=now),
            _obs(5, OwnerPresence.AWAY, now=now),
        ],
        now=now,
    )
    task_id = _ready_task(queue)
    calls: list[str] = []

    async def effect() -> object:
        calls.append("run")
        return {"ok": True}

    result = asyncio.run(
        guard.dispatch(
            task_id=task_id,
            work_kind=BackgroundWorkKind.READING_RESEARCH,
            effect=effect,
        )
    )

    assert result.action is BackgroundAction.RUN
    assert result.executed is True
    assert result.effect_result == {"ok": True}
    assert calls == ["run"]
    assert queue.get(task_id).state is TaskState.READY
    assert resources.active_count(scope="background_life", owner_id="living-agent") == 0
    events = audit.list_for(entity_type="task", entity_id=task_id)
    assert [event.event_type for event in events][-2:] == [
        "background.dispatch_permitted",
        "background.dispatch_returned",
    ]


def test_owner_active_preflight_durably_pauses_without_effect(tmp_path: Path) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    store = _store(tmp_path)
    guard, queue, _audit, _resources = _guard(
        store=store,
        observations=[_obs(1, OwnerPresence.ACTIVE, now=now)],
        now=now,
    )
    task_id = _ready_task(queue)
    called = False

    async def effect() -> object:
        nonlocal called
        called = True
        return None

    result = asyncio.run(
        guard.dispatch(
            task_id=task_id,
            work_kind=BackgroundWorkKind.SELF_TEST,
            effect=effect,
        )
    )

    assert result.action is BackgroundAction.PAUSE
    assert result.reason == "owner_active"
    assert called is False
    assert queue.get(task_id).state is TaskState.PAUSED


def test_owner_return_between_preflight_and_effect_recheck_blocks_dispatch(
    tmp_path: Path,
) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    store = _store(tmp_path)
    guard, queue, _audit, resources = _guard(
        store=store,
        observations=[
            _obs(10, OwnerPresence.AWAY, now=now),
            _obs(11, OwnerPresence.ACTIVE, now=now),
        ],
        now=now,
    )
    task_id = _ready_task(queue)

    async def effect() -> object:
        raise AssertionError("effect must not run")

    result = asyncio.run(
        guard.dispatch(
            task_id=task_id,
            work_kind=BackgroundWorkKind.UNFINISHED_WORK,
            effect=effect,
        )
    )

    assert result.action is BackgroundAction.PAUSE
    assert queue.get(task_id).state is TaskState.PAUSED
    assert resources.active_count(scope="background_life", owner_id="living-agent") == 0


def test_owner_return_at_effect_commit_blocks_dispatch(tmp_path: Path) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    store = _store(tmp_path)
    guard, queue, _audit, resources = _guard(
        store=store,
        observations=[
            _obs(20, OwnerPresence.AWAY, now=now),
            _obs(21, OwnerPresence.AWAY, now=now),
            _obs(22, OwnerPresence.ACTIVE, now=now),
        ],
        now=now,
    )
    task_id = _ready_task(queue)

    async def effect() -> object:
        raise AssertionError("effect must not run")

    result = asyncio.run(
        guard.dispatch(
            task_id=task_id,
            work_kind=BackgroundWorkKind.EVIDENCE_VERIFICATION,
            effect=effect,
        )
    )

    assert result.action is BackgroundAction.PAUSE
    assert queue.get(task_id).state is TaskState.PAUSED
    assert resources.active_count(scope="background_life", owner_id="living-agent") == 0


def test_owner_return_after_resource_grant_is_fenced_and_releases_capacity(
    tmp_path: Path,
) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    store = _store(tmp_path)
    guard, queue, _audit, resources = _guard(
        store=store,
        observations=[
            _obs(25, OwnerPresence.AWAY, now=now),
            _obs(26, OwnerPresence.AWAY, now=now),
            _obs(27, OwnerPresence.AWAY, now=now),
            _obs(28, OwnerPresence.ACTIVE, now=now),
        ],
        now=now,
    )
    task_id = _ready_task(queue)

    async def effect() -> object:
        raise AssertionError("effect must not run")

    result = asyncio.run(
        guard.dispatch(
            task_id=task_id,
            work_kind=BackgroundWorkKind.EVIDENCE_VERIFICATION,
            effect=effect,
        )
    )

    assert result.action is BackgroundAction.PAUSE
    assert result.reason == "owner_active"
    assert queue.get(task_id).state is TaskState.PAUSED
    assert resources.active_count(scope="background_life", owner_id="living-agent") == 0
    assert resources.queued(scope="background_life", owner_id="living-agent") == ()


def test_owner_return_after_post_grant_resource_recheck_blocks_effect(
    tmp_path: Path,
) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    store = _store(tmp_path)
    guard, queue, _audit, resources = _guard(
        store=store,
        observations=[
            _obs(30, OwnerPresence.AWAY, now=now),
            _obs(31, OwnerPresence.AWAY, now=now),
            _obs(32, OwnerPresence.AWAY, now=now),
            _obs(33, OwnerPresence.AWAY, now=now),
            _obs(34, OwnerPresence.ACTIVE, now=now),
        ],
        now=now,
    )
    task_id = _ready_task(queue)

    async def effect() -> object:
        raise AssertionError("effect must not run after owner returns")

    result = asyncio.run(
        guard.dispatch(
            task_id=task_id,
            work_kind=BackgroundWorkKind.EVIDENCE_VERIFICATION,
            effect=effect,
        )
    )

    assert result.action is BackgroundAction.PAUSE
    assert result.reason == "owner_active"
    assert result.executed is False
    assert queue.get(task_id).state is TaskState.PAUSED
    assert resources.active_count(scope="background_life", owner_id="living-agent") == 0
    assert IdempotencyLedger(store).list_for_task(task_id) == ()


def test_resource_pressure_race_at_final_admission_defers_without_effect(
    tmp_path: Path,
) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    store = _store(tmp_path)
    resource_observer = SequenceResourceObserver([10.0, 10.0, 10.0, 95.0])
    guard, queue, _audit, manager = _guard(
        store=store,
        observations=[
            _obs(30, OwnerPresence.AWAY, now=now),
            _obs(31, OwnerPresence.AWAY, now=now),
            _obs(32, OwnerPresence.AWAY, now=now),
        ],
        now=now,
        resource_observer=resource_observer,
    )
    task_id = _ready_task(queue)

    async def effect() -> object:
        raise AssertionError("effect must not run")

    result = asyncio.run(
        guard.dispatch(
            task_id=task_id,
            work_kind=BackgroundWorkKind.READING_RESEARCH,
            effect=effect,
        )
    )

    assert result.action is BackgroundAction.DEFER
    assert result.reason == "cpu_limit"
    assert queue.get(task_id).state is TaskState.READY
    assert manager.active_count(scope="background_life", owner_id="living-agent") == 0
    assert manager.queued(scope="background_life", owner_id="living-agent") == ()



def test_resource_pressure_after_grant_is_rechecked_before_effect(tmp_path: Path) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    store = _store(tmp_path)
    resource_observer = SequenceResourceObserver(
        [10.0, 10.0, 10.0, 10.0, 95.0]
    )
    guard, queue, _audit, manager = _guard(
        store=store,
        observations=[
            _obs(35, OwnerPresence.AWAY, now=now),
            _obs(36, OwnerPresence.AWAY, now=now),
            _obs(37, OwnerPresence.AWAY, now=now),
            _obs(38, OwnerPresence.AWAY, now=now),
        ],
        now=now,
        resource_observer=resource_observer,
    )
    task_id = _ready_task(queue)

    async def effect() -> object:
        raise AssertionError("effect must not run")

    result = asyncio.run(
        guard.dispatch(
            task_id=task_id,
            work_kind=BackgroundWorkKind.READING_RESEARCH,
            effect=effect,
        )
    )

    assert result.action is BackgroundAction.DEFER
    assert result.reason == "resource_pressure"
    assert queue.get(task_id).state is TaskState.READY
    assert manager.active_count(scope="background_life", owner_id="living-agent") == 0
    assert IdempotencyLedger(store).list_for_task(task_id) == ()


def test_high_impact_power_change_after_grant_blocks_effect(tmp_path: Path) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    store = _store(tmp_path)
    resource_observer = SequenceResourceObserver(
        [10.0, 10.0, 10.0, 10.0, 10.0],
        [True, True, True, True, False],
    )
    guard, queue, _audit, manager = _guard(
        store=store,
        observations=[
            _obs(45, OwnerPresence.AWAY, now=now),
            _obs(46, OwnerPresence.AWAY, now=now),
            _obs(47, OwnerPresence.AWAY, now=now),
            _obs(48, OwnerPresence.AWAY, now=now),
        ],
        now=now,
        resource_observer=resource_observer,
    )
    task_id = _ready_task(queue)

    async def effect() -> object:
        raise AssertionError("effect must not run")

    result = asyncio.run(
        guard.dispatch(
            task_id=task_id,
            work_kind=BackgroundWorkKind.EVALUATION,
            effect=effect,
        )
    )

    assert result.action is BackgroundAction.DEFER
    assert result.reason == "battery_power"
    assert queue.get(task_id).state is TaskState.READY
    assert manager.active_count(scope="background_life", owner_id="living-agent") == 0
    assert IdempotencyLedger(store).list_for_task(task_id) == ()

def test_stale_presence_fails_closed_and_records_only_error_type(tmp_path: Path) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    store = _store(tmp_path)
    guard, queue, audit, _resources = _guard(
        store=store,
        observations=[_obs(40, OwnerPresence.AWAY, now=now, age_seconds=30.0)],
        now=now,
    )
    task_id = _ready_task(queue)

    async def effect() -> object:
        raise AssertionError("effect must not run")

    result = asyncio.run(
        guard.dispatch(
            task_id=task_id,
            work_kind=BackgroundWorkKind.SELF_TEST,
            effect=effect,
        )
    )

    assert result.reason == "owner_presence_untrusted"
    assert queue.get(task_id).state is TaskState.PAUSED
    rejected = [
        event
        for event in audit.list_for(entity_type="task", entity_id=task_id)
        if event.event_type == "background.owner_presence_rejected"
    ]
    assert rejected[-1].payload["error_type"] == "OwnerPresenceEvidenceError"
    assert "error" not in rejected[-1].payload


def test_wrong_presence_source_fails_closed(tmp_path: Path) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    store = _store(tmp_path)
    guard, queue, _audit, _resources = _guard(
        store=store,
        observations=[
            _obs(
                50,
                OwnerPresence.AWAY,
                now=now,
                source_id="untrusted-observer",
            )
        ],
        now=now,
    )
    task_id = _ready_task(queue)

    async def effect() -> object:
        raise AssertionError("effect must not run")

    result = asyncio.run(
        guard.dispatch(
            task_id=task_id,
            work_kind=BackgroundWorkKind.SELF_TEST,
            effect=effect,
        )
    )

    assert result.action is BackgroundAction.PAUSE
    assert queue.get(task_id).state is TaskState.PAUSED


def test_externally_paused_task_cannot_be_auto_resumed(
    tmp_path: Path,
) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    store = _store(tmp_path)
    guard, queue, _audit, resources = _guard(
        store=store,
        observations=[],
        now=now,
    )
    task_id = _ready_task(queue)
    queue.transition(task_id, TaskState.PAUSED)

    async def effect() -> object:
        raise AssertionError("externally paused work must not run")

    with pytest.raises(ValueError, match="does not own"):
        asyncio.run(
            guard.dispatch(
                task_id=task_id,
                work_kind=BackgroundWorkKind.MEMORY_CONSOLIDATION,
                effect=effect,
            )
        )

    assert queue.get(task_id).state is TaskState.PAUSED
    assert resources.active_count(scope="background_life", owner_id="living-agent") == 0
    assert IdempotencyLedger(store).list_for_task(task_id) == ()


def test_later_external_pause_invalidates_old_background_pause_ownership(
    tmp_path: Path,
) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    store = _store(tmp_path)
    first, queue, audit, _resources = _guard(
        store=store,
        observations=[_obs(58, OwnerPresence.ACTIVE, now=now)],
        now=now,
    )
    task_id = _ready_task(queue)

    async def never() -> object:
        raise AssertionError("paused work must not run")

    paused = asyncio.run(
        first.dispatch(
            task_id=task_id,
            work_kind=BackgroundWorkKind.MEMORY_CONSOLIDATION,
            effect=never,
        )
    )
    assert paused.action is BackgroundAction.PAUSE

    pause_markers = [
        event
        for event in audit.list_for(entity_type="task", entity_id=task_id)
        if event.event_type == "background.dispatch_paused"
    ]
    assert len(pause_markers) == 1
    marker_event_id = pause_markers[0].payload["task_event_id"]
    assert type(marker_event_id) is int

    queue.transition(task_id, TaskState.READY)
    queue.transition(task_id, TaskState.PAUSED)

    second, _, _, resources = _guard(
        store=store,
        observations=[],
        now=now,
    )
    with pytest.raises(ValueError, match="does not own"):
        asyncio.run(
            second.dispatch(
                task_id=task_id,
                work_kind=BackgroundWorkKind.MEMORY_CONSOLIDATION,
                effect=never,
            )
        )

    assert queue.get(task_id).state is TaskState.PAUSED
    assert resources.active_count(scope="background_life", owner_id="living-agent") == 0


def test_restart_rejects_replayed_presence_then_new_evidence_resumes(
    tmp_path: Path,
) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    store = _store(tmp_path)
    first, queue, _audit, _resources = _guard(
        store=store,
        observations=[_obs(60, OwnerPresence.ACTIVE, now=now)],
        now=now,
    )
    task_id = _ready_task(queue)

    async def never() -> object:
        raise AssertionError("effect must not run")

    first_result = asyncio.run(
        first.dispatch(
            task_id=task_id,
            work_kind=BackgroundWorkKind.MEMORY_CONSOLIDATION,
            effect=never,
        )
    )
    assert first_result.action is BackgroundAction.PAUSE
    assert queue.get(task_id).state is TaskState.PAUSED
    pause_marker = next(
        event
        for event in _audit.list_for(entity_type="task", entity_id=task_id)
        if event.event_type == "background.dispatch_paused"
    )
    with store.connection() as conn:
        latest_task_event = conn.execute(
            "SELECT event_id FROM task_events WHERE task_id = ? "
            "ORDER BY event_id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    assert latest_task_event is not None
    assert pause_marker.payload["task_event_id"] == int(latest_task_event["event_id"])

    replay, _, _, _ = _guard(
        store=store,
        observations=[_obs(60, OwnerPresence.AWAY, now=now)],
        now=now,
    )
    replay_result = asyncio.run(
        replay.dispatch(
            task_id=task_id,
            work_kind=BackgroundWorkKind.MEMORY_CONSOLIDATION,
            effect=never,
        )
    )
    assert replay_result.reason == "owner_presence_untrusted"
    assert queue.get(task_id).state is TaskState.PAUSED

    resumed, _, _, _ = _guard(
        store=store,
        observations=[
            _obs(61, OwnerPresence.AWAY, now=now),
            _obs(62, OwnerPresence.AWAY, now=now),
            _obs(63, OwnerPresence.AWAY, now=now),
            _obs(64, OwnerPresence.AWAY, now=now),
            _obs(65, OwnerPresence.AWAY, now=now),
        ],
        now=now,
    )
    calls: list[str] = []

    async def effect() -> object:
        calls.append("resumed")
        return "ok"

    resumed_result = asyncio.run(
        resumed.dispatch(
            task_id=task_id,
            work_kind=BackgroundWorkKind.MEMORY_CONSOLIDATION,
            effect=effect,
        )
    )

    assert resumed_result.executed is True
    assert calls == ["resumed"]
    assert queue.get(task_id).state is TaskState.READY


def test_timestamp_regression_is_rejected_even_when_sequence_advances(
    tmp_path: Path,
) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    store = _store(tmp_path)
    first, queue, _audit, _resources = _guard(
        store=store,
        observations=[_obs(70, OwnerPresence.ACTIVE, now=now, age_seconds=1.0)],
        now=now,
    )
    task_id = _ready_task(queue)

    async def never() -> object:
        raise AssertionError("effect must not run")

    asyncio.run(
        first.dispatch(
            task_id=task_id,
            work_kind=BackgroundWorkKind.SELF_TEST,
            effect=never,
        )
    )

    regressed, _, _, _ = _guard(
        store=store,
        observations=[_obs(71, OwnerPresence.AWAY, now=now, age_seconds=2.0)],
        now=now,
    )
    result = asyncio.run(
        regressed.dispatch(
            task_id=task_id,
            work_kind=BackgroundWorkKind.SELF_TEST,
            effect=never,
        )
    )

    assert result.reason == "owner_presence_untrusted"
    assert queue.get(task_id).state is TaskState.PAUSED


def test_effect_exception_still_releases_resource_grant(tmp_path: Path) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    store = _store(tmp_path)
    guard, queue, _audit, resources = _guard(
        store=store,
        observations=[
            _obs(80, OwnerPresence.AWAY, now=now),
            _obs(81, OwnerPresence.AWAY, now=now),
            _obs(82, OwnerPresence.AWAY, now=now),
            _obs(83, OwnerPresence.AWAY, now=now),
            _obs(84, OwnerPresence.AWAY, now=now),
        ],
        now=now,
    )
    task_id = _ready_task(queue)

    async def effect() -> object:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(
            guard.dispatch(
                task_id=task_id,
                work_kind=BackgroundWorkKind.EVIDENCE_VERIFICATION,
                effect=effect,
            )
        )

    assert resources.active_count(scope="background_life", owner_id="living-agent") == 0


class CompletingRuntime:
    runtime_id = "background-test-runtime"
    capabilities = frozenset()

    async def run(self, request: RuntimeRequest) -> RuntimeResult:
        return RuntimeResult(
            outcome=RuntimeOutcome.COMPLETED,
            output={"task_id": request.task_id},
        )

    async def resume(self, request) -> RuntimeResult:  # pragma: no cover - not used here
        raise AssertionError("resume must not be called")

    async def cancel(self, *, task_id: str, thread_id: str) -> bool:
        return False


def test_guard_composes_with_canonical_task_runtime_coordinator(tmp_path: Path) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    store = _store(tmp_path)
    guard, queue, audit, resources = _guard(
        store=store,
        observations=[
            _obs(90, OwnerPresence.AWAY, now=now),
            _obs(91, OwnerPresence.AWAY, now=now),
            _obs(92, OwnerPresence.AWAY, now=now),
            _obs(93, OwnerPresence.AWAY, now=now),
            _obs(94, OwnerPresence.AWAY, now=now),
        ],
        now=now,
    )
    task_id = _ready_task(queue)
    coordinator = TaskRuntimeCoordinator(queue, audit)
    runtime = CompletingRuntime()

    async def effect() -> object:
        return await coordinator.start(
            runtime,
            RuntimeRequest(task_id=task_id, thread_id="background-thread"),
        )

    result = asyncio.run(
        guard.dispatch(
            task_id=task_id,
            work_kind=BackgroundWorkKind.UNFINISHED_WORK,
            effect=effect,
        )
    )

    assert result.executed is True
    assert isinstance(result.effect_result, RuntimeResult)
    assert result.effect_result.outcome is RuntimeOutcome.COMPLETED
    assert queue.get(task_id).state is TaskState.COMPLETED
    assert resources.active_count(scope="background_life", owner_id="living-agent") == 0
    event_types = [
        event.event_type for event in audit.list_for(entity_type="task", entity_id=task_id)
    ]
    assert "background.dispatch_permitted" in event_types
    assert "runtime.started" in event_types
    assert "runtime.finished" in event_types
    assert "background.dispatch_returned" in event_types


def test_presence_observation_rejects_behavioral_tzinfo_before_execution() -> None:
    zone = _BehavioralTimezone()

    with pytest.raises(ValueError, match="UTC"):
        OwnerPresenceObservation(
            source_id="source",
            sequence=1,
            presence=OwnerPresence.AWAY,
            observed_at=datetime(2030, 1, 1, tzinfo=zone),
        )

    assert zone.calls == 0


def test_presence_observation_requires_exact_utc_carrier() -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)

    with pytest.raises(TypeError, match="sequence"):
        OwnerPresenceObservation(
            source_id="source",
            sequence=True,  # type: ignore[arg-type]
            presence=OwnerPresence.AWAY,
            observed_at=now,
        )

    with pytest.raises(ValueError, match="UTC"):
        OwnerPresenceObservation(
            source_id="source",
            sequence=1,
            presence=OwnerPresence.AWAY,
            observed_at=datetime(2030, 1, 1, tzinfo=timezone(timedelta(hours=1))),
        )


def test_presence_clock_rejects_behavioral_tzinfo_before_execution(
    tmp_path: Path,
) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    zone = _BehavioralTimezone()
    bad_now = datetime(2030, 1, 1, tzinfo=zone)
    store = _store(tmp_path)
    queue = TaskQueue(store)
    audit = AuditLog(store)
    resources = ResourceManager(store, SequenceResourceObserver())
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
        presence=SequencePresence([_obs(1, OwnerPresence.AWAY, now=now)]),
        source_id="win32-owner-presence",
        clock=lambda: bad_now,
    )
    task_id = _ready_task(queue)
    calls = 0

    async def effect() -> object:
        nonlocal calls
        calls += 1
        return "must-not-run"

    result = asyncio.run(
        guard.dispatch(
            task_id=task_id,
            work_kind=BackgroundWorkKind.READING_RESEARCH,
            effect=effect,
        )
    )

    assert result.action is BackgroundAction.PAUSE
    assert result.executed is False
    assert calls == 0
    assert zone.calls == 0
    assert queue.get(task_id).state is TaskState.PAUSED


def test_guard_rejects_negative_future_skew(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="non-negative"):
        BackgroundDispatchGuard(
            queue=TaskQueue(store),
            audit=AuditLog(store),
            resources=ResourceManager(store, SequenceResourceObserver()),
            presence=SequencePresence([]),
            source_id="win32-owner-presence",
            max_future_skew_seconds=-0.1,
        )


class IncrementingPresence:
    def __init__(self, *, now: datetime) -> None:
        self._now = now
        self._sequence = 0

    def observe(self) -> OwnerPresenceObservation:
        self._sequence += 1
        return _obs(self._sequence, OwnerPresence.AWAY, now=self._now)


def test_concurrent_dispatch_for_same_task_has_one_durable_effect_winner(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        now = datetime(2030, 1, 1, tzinfo=UTC)
        store = _store(tmp_path)
        queue = TaskQueue(store)
        audit = AuditLog(store)
        resources = ResourceManager(store, SequenceResourceObserver())
        resources.set_budget(
            ResourceBudget(
                scope="background_life",
                owner_id="living-agent",
                max_concurrent=2,
                max_cpu_percent=80.0,
                max_memory_percent=80.0,
            )
        )
        guard = BackgroundDispatchGuard(
            queue=queue,
            audit=audit,
            resources=resources,
            presence=IncrementingPresence(now=now),
            source_id="win32-owner-presence",
            clock=lambda: now,
        )
        task_id = _ready_task(queue)
        entered = asyncio.Event()
        release = asyncio.Event()
        calls: list[str] = []

        async def first_effect() -> object:
            calls.append("first")
            entered.set()
            await release.wait()
            return "done"

        async def second_effect() -> object:
            calls.append("second")
            return "should-not-run"

        first = asyncio.create_task(
            guard.dispatch(
                task_id=task_id,
                work_kind=BackgroundWorkKind.UNFINISHED_WORK,
                effect=first_effect,
            )
        )
        await entered.wait()

        with pytest.raises(IdempotencyConflictError, match="pending or uncertain"):
            await guard.dispatch(
                task_id=task_id,
                work_kind=BackgroundWorkKind.UNFINISHED_WORK,
                effect=second_effect,
            )

        release.set()
        result = await first
        assert result.executed is True
        assert calls == ["first"]

        records = [
            record
            for record in IdempotencyLedger(store).list_for_task(task_id)
            if record.operation_type == "background.dispatch"
        ]
        assert len(records) == 1
        assert records[0].status is IdempotencyStatus.COMPLETED
        assert records[0].result == {
            "effect_started": True,
            "work_kind": BackgroundWorkKind.UNFINISHED_WORK.value,
        }

    asyncio.run(scenario())


def test_completed_dispatch_claim_blocks_replay_when_task_state_did_not_advance(
    tmp_path: Path,
) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    store = _store(tmp_path)
    guard, queue, audit, _resources = _guard(
        store=store,
        observations=[
            _obs(101, OwnerPresence.AWAY, now=now),
            _obs(102, OwnerPresence.AWAY, now=now),
            _obs(103, OwnerPresence.AWAY, now=now),
            _obs(104, OwnerPresence.AWAY, now=now),
            _obs(105, OwnerPresence.AWAY, now=now),
            _obs(106, OwnerPresence.AWAY, now=now),
            _obs(107, OwnerPresence.AWAY, now=now),
            _obs(108, OwnerPresence.AWAY, now=now),
        ],
        now=now,
    )
    task_id = _ready_task(queue)
    calls: list[str] = []

    async def effect() -> object:
        calls.append("run")
        return "ok"

    first = asyncio.run(
        guard.dispatch(
            task_id=task_id,
            work_kind=BackgroundWorkKind.READING_RESEARCH,
            effect=effect,
        )
    )
    second = asyncio.run(
        guard.dispatch(
            task_id=task_id,
            work_kind=BackgroundWorkKind.READING_RESEARCH,
            effect=effect,
        )
    )

    assert first.executed is True
    assert second.action is BackgroundAction.DEFER
    assert second.reason == "dispatch_already_completed"
    assert second.executed is False
    assert calls == ["run"]
    assert any(
        event.event_type == "background.dispatch_duplicate_blocked"
        for event in audit.list_for(entity_type="task", entity_id=task_id)
    )


def test_deferred_effect_result_cannot_escape_authority_window(tmp_path: Path) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    store = _store(tmp_path)
    guard, queue, audit, resources = _guard(
        store=store,
        observations=[
            _obs(111, OwnerPresence.AWAY, now=now),
            _obs(112, OwnerPresence.AWAY, now=now),
            _obs(113, OwnerPresence.AWAY, now=now),
            _obs(114, OwnerPresence.AWAY, now=now),
            _obs(115, OwnerPresence.AWAY, now=now),
        ],
        now=now,
    )
    task_id = _ready_task(queue)

    async def later() -> object:
        return "late"

    async def effect() -> object:
        return later()

    with pytest.raises(TypeError, match="deferred execution"):
        asyncio.run(
            guard.dispatch(
                task_id=task_id,
                work_kind=BackgroundWorkKind.EVIDENCE_VERIFICATION,
                effect=effect,
            )
        )

    records = [
        record
        for record in IdempotencyLedger(store).list_for_task(task_id)
        if record.operation_type == "background.dispatch"
    ]
    assert len(records) == 1
    assert records[0].status is IdempotencyStatus.UNCERTAIN
    assert resources.active_count(scope="background_life", owner_id="living-agent") == 0
    uncertain = [
        event
        for event in audit.list_for(entity_type="task", entity_id=task_id)
        if event.event_type == "background.dispatch_uncertain"
    ]
    assert uncertain[-1].payload["error_type"] == "DeferredEffectResult"



def test_mutated_exact_presence_carrier_is_resnapshotted_and_rejected(
    tmp_path: Path,
) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    store = _store(tmp_path)
    observation = _obs(201, OwnerPresence.AWAY, now=now)
    object.__setattr__(observation, "sequence", True)

    guard, queue, audit, _resources = _guard(
        store=store,
        observations=[observation],
        now=now,
    )
    task_id = _ready_task(queue)

    async def effect() -> object:
        raise AssertionError("effect must not run")

    result = asyncio.run(
        guard.dispatch(
            task_id=task_id,
            work_kind=BackgroundWorkKind.SELF_TEST,
            effect=effect,
        )
    )

    assert result.action is BackgroundAction.PAUSE
    assert result.reason == "owner_presence_untrusted"
    assert queue.get(task_id).state is TaskState.PAUSED
    rejected = [
        event
        for event in audit.list_for(entity_type="task", entity_id=task_id)
        if event.event_type == "background.owner_presence_rejected"
    ]
    assert rejected[-1].payload["error_type"] == "OwnerPresenceEvidenceError"


def test_stored_non_utc_presence_evidence_fails_closed(tmp_path: Path) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    store = _store(tmp_path)
    audit = AuditLog(store)
    audit.append(
        event_type="background.owner_presence_observed",
        entity_type="owner_presence_source",
        entity_id="win32-owner-presence",
        payload={
            "task_id": "historical",
            "phase": "preflight",
            "sequence": 210,
            "presence": OwnerPresence.AWAY.value,
            "observed_at": "2030-01-01T01:00:00+01:00",
        },
    )
    guard, queue, _audit, _resources = _guard(
        store=store,
        observations=[_obs(211, OwnerPresence.AWAY, now=now)],
        now=now,
    )
    task_id = _ready_task(queue)

    async def effect() -> object:
        raise AssertionError("effect must not run")

    result = asyncio.run(
        guard.dispatch(
            task_id=task_id,
            work_kind=BackgroundWorkKind.SELF_TEST,
            effect=effect,
        )
    )

    assert result.reason == "owner_presence_untrusted"
    assert queue.get(task_id).state is TaskState.PAUSED


class WrongOwnerStatusResourceManager(ResourceManager):
    def status(self, *, scope: str, owner_id: str):
        status = super().status(scope=scope, owner_id=owner_id)
        return type(status)(
            budget=ResourceBudget(
                scope=status.budget.scope,
                owner_id="different-owner",
                max_concurrent=status.budget.max_concurrent,
                max_cpu_percent=status.budget.max_cpu_percent,
                max_memory_percent=status.budget.max_memory_percent,
            ),
            snapshot=status.snapshot,
            active_count=status.active_count,
            queued_count=status.queued_count,
            concurrency_headroom=status.concurrency_headroom,
            cpu_headroom_percent=status.cpu_headroom_percent,
            memory_headroom_percent=status.memory_headroom_percent,
            pressure_reasons=status.pressure_reasons,
        )


def test_resource_status_must_bind_to_exact_background_owner(tmp_path: Path) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    store = _store(tmp_path)
    queue = TaskQueue(store)
    audit = AuditLog(store)
    resources = WrongOwnerStatusResourceManager(store, SequenceResourceObserver())
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
        presence=SequencePresence([_obs(220, OwnerPresence.AWAY, now=now)]),
        source_id="win32-owner-presence",
        clock=lambda: now,
    )
    task_id = _ready_task(queue)

    async def effect() -> object:
        raise AssertionError("effect must not run")

    with pytest.raises(ValueError, match="owner_id"):
        asyncio.run(
            guard.dispatch(
                task_id=task_id,
                work_kind=BackgroundWorkKind.SELF_TEST,
                effect=effect,
            )
        )

    assert queue.get(task_id).state is TaskState.READY


def test_presence_source_identity_is_bounded(tmp_path: Path) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    store = _store(tmp_path)

    with pytest.raises(ValueError, match="too long"):
        BackgroundDispatchGuard(
            queue=TaskQueue(store),
            audit=AuditLog(store),
            resources=ResourceManager(store, SequenceResourceObserver()),
            presence=SequencePresence([]),
            source_id="x" * 257,
            clock=lambda: now,
        )

    with pytest.raises(ValueError, match="too long"):
        OwnerPresenceObservation(
            source_id="x" * 257,
            sequence=1,
            presence=OwnerPresence.AWAY,
            observed_at=now,
        )



def test_runtime_identities_reject_non_utf8_before_durable_boundaries(
    tmp_path: Path,
) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    store = _store(tmp_path)
    bad = "identity-\ud800"

    with pytest.raises(ValueError, match="source_id must be valid UTF-8 text"):
        BackgroundDispatchGuard(
            queue=TaskQueue(store),
            audit=AuditLog(store),
            resources=ResourceManager(store, SequenceResourceObserver()),
            presence=SequencePresence([]),
            source_id=bad,
            clock=lambda: now,
        )

    with pytest.raises(ValueError, match="source_id must be valid UTF-8 text"):
        OwnerPresenceObservation(
            source_id=bad,
            sequence=1,
            presence=OwnerPresence.AWAY,
            observed_at=now,
        )

    guard, queue, audit, _resources = _guard(
        store=store,
        observations=[],
        now=now,
    )
    task_id = _ready_task(queue)
    calls = 0

    async def effect() -> object:
        nonlocal calls
        calls += 1
        return "must-not-run"

    with pytest.raises(ValueError, match="owner_id must be valid UTF-8 text"):
        asyncio.run(
            guard.dispatch(
                task_id=task_id,
                work_kind=BackgroundWorkKind.SELF_TEST,
                effect=effect,
                owner_id=bad,
            )
        )

    with pytest.raises(ValueError, match="task_id must be valid UTF-8 text"):
        asyncio.run(
            guard.dispatch(
                task_id=bad,
                work_kind=BackgroundWorkKind.SELF_TEST,
                effect=effect,
            )
        )

    assert calls == 0
    assert queue.get(task_id).state is TaskState.READY
    assert audit.list_for(entity_type="task", entity_id=task_id) == ()


class ExplodingResourceObserver:
    def snapshot(self) -> ResourceSnapshot:
        raise AssertionError("resource telemetry must not be read")


@pytest.mark.parametrize("presence", [OwnerPresence.ACTIVE, OwnerPresence.UNKNOWN])
def test_present_owner_pauses_before_resource_telemetry(
    tmp_path: Path,
    presence: OwnerPresence,
) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    store = _store(tmp_path)
    queue = TaskQueue(store)
    audit = AuditLog(store)
    resources = ResourceManager(store, ExplodingResourceObserver())
    guard = BackgroundDispatchGuard(
        queue=queue,
        audit=audit,
        resources=resources,
        presence=SequencePresence([_obs(230, presence, now=now)]),
        source_id="win32-owner-presence",
        clock=lambda: now,
    )
    task_id = _ready_task(queue)

    async def effect() -> object:
        raise AssertionError("effect must not run")

    result = asyncio.run(
        guard.dispatch(
            task_id=task_id,
            work_kind=BackgroundWorkKind.BOUNDED_ML_PILOT,
            effect=effect,
        )
    )

    assert result.action is BackgroundAction.PAUSE
    assert result.reason in {"owner_active", "owner_presence_unknown"}
    assert queue.get(task_id).state is TaskState.PAUSED


@pytest.mark.parametrize("max_age", [60.0001, 1_000_000.0])
def test_presence_freshness_window_cannot_disable_staleness(
    tmp_path: Path,
    max_age: float,
) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError, match=r"\(0, 60\]"):
        BackgroundDispatchGuard(
            queue=TaskQueue(store),
            audit=AuditLog(store),
            resources=ResourceManager(store, SequenceResourceObserver()),
            presence=SequencePresence([]),
            source_id="win32-owner-presence",
            max_presence_age_seconds=max_age,
        )


def test_future_skew_cannot_exceed_freshness_window(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="must not exceed"):
        BackgroundDispatchGuard(
            queue=TaskQueue(store),
            audit=AuditLog(store),
            resources=ResourceManager(store, SequenceResourceObserver()),
            presence=SequencePresence([]),
            source_id="win32-owner-presence",
            max_presence_age_seconds=5.0,
            max_future_skew_seconds=5.1,
        )



class SimulatedBackgroundProcessLoss(BaseException):
    pass


def test_process_loss_after_effect_start_leaves_nonreplayable_pending_claim(
    tmp_path: Path,
) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    store = _store(tmp_path)
    guard, queue, _audit, resources = _guard(
        store=store,
        observations=[
            _obs(240, OwnerPresence.AWAY, now=now),
            _obs(241, OwnerPresence.AWAY, now=now),
            _obs(242, OwnerPresence.AWAY, now=now),
            _obs(243, OwnerPresence.AWAY, now=now),
            _obs(244, OwnerPresence.AWAY, now=now),
            _obs(245, OwnerPresence.AWAY, now=now),
            _obs(246, OwnerPresence.AWAY, now=now),
            _obs(247, OwnerPresence.AWAY, now=now),
        ],
        now=now,
    )
    task_id = _ready_task(queue)

    async def crash() -> object:
        raise SimulatedBackgroundProcessLoss()

    with pytest.raises(SimulatedBackgroundProcessLoss):
        asyncio.run(
            guard.dispatch(
                task_id=task_id,
                work_kind=BackgroundWorkKind.UNFINISHED_WORK,
                effect=crash,
            )
        )

    records = [
        record
        for record in IdempotencyLedger(store).list_for_task(task_id)
        if record.operation_type == "background.dispatch"
    ]
    assert len(records) == 1
    assert records[0].status is IdempotencyStatus.PENDING
    assert resources.active_count(scope="background_life", owner_id="living-agent") == 0

    async def replay() -> object:
        raise AssertionError("replay effect must not run")

    with pytest.raises(IdempotencyConflictError, match="pending or uncertain"):
        asyncio.run(
            guard.dispatch(
                task_id=task_id,
                work_kind=BackgroundWorkKind.UNFINISHED_WORK,
                effect=replay,
            )
        )


def test_resume_paused_requires_canonical_ready_running_prefix(tmp_path: Path) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    store = _store(tmp_path)
    guard, queue, audit, resources = _guard(
        store=store,
        observations=[
            _obs(300, OwnerPresence.AWAY, now=now),
            _obs(301, OwnerPresence.AWAY, now=now),
            _obs(302, OwnerPresence.AWAY, now=now),
            _obs(303, OwnerPresence.AWAY, now=now),
            _obs(304, OwnerPresence.AWAY, now=now),
        ],
        now=now,
    )
    task_id, pause_event_id = _owner_return_paused_task(queue, audit)

    async def effect() -> object:
        queue.transition(task_id, TaskState.READY)
        queue.transition(task_id, TaskState.RUNNING)
        return "continued"

    result = asyncio.run(
        guard.resume_paused(
            task_id=task_id,
            work_kind=BackgroundWorkKind.UNFINISHED_WORK,
            effect=effect,
        )
    )

    assert result.action is BackgroundAction.RUN
    assert result.executed is True
    assert result.effect_result == "continued"
    records = [
        record
        for record in IdempotencyLedger(store).list_for_task(task_id)
        if record.operation_type == "background.resume"
    ]
    assert len(records) == 1
    assert records[0].status is IdempotencyStatus.COMPLETED
    assert records[0].input_fingerprint
    assert queue.get(task_id).state is TaskState.RUNNING
    assert resources.active_count(scope="background_life", owner_id="living-agent") == 0
    events = audit.list_for(entity_type="task", entity_id=task_id)
    returned = [event for event in events if event.event_type == "background.resume_returned"]
    assert len(returned) == 1
    assert returned[0].payload["pause_event_id"] == pause_event_id


def test_resume_paused_noop_effect_becomes_uncertain_not_pending(tmp_path: Path) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    store = _store(tmp_path)
    guard, queue, audit, resources = _guard(
        store=store,
        observations=[
            _obs(310, OwnerPresence.AWAY, now=now),
            _obs(311, OwnerPresence.AWAY, now=now),
            _obs(312, OwnerPresence.AWAY, now=now),
            _obs(313, OwnerPresence.AWAY, now=now),
            _obs(314, OwnerPresence.AWAY, now=now),
            _obs(315, OwnerPresence.AWAY, now=now),
            _obs(316, OwnerPresence.AWAY, now=now),
            _obs(317, OwnerPresence.AWAY, now=now),
        ],
        now=now,
    )
    task_id, _pause_event_id = _owner_return_paused_task(queue, audit)
    calls = 0

    async def effect() -> object:
        nonlocal calls
        calls += 1
        return "returned-without-transition"

    with pytest.raises(RuntimeError, match="PAUSED -> READY -> RUNNING"):
        asyncio.run(
            guard.resume_paused(
                task_id=task_id,
                work_kind=BackgroundWorkKind.UNFINISHED_WORK,
                effect=effect,
            )
        )

    assert calls == 1
    records = [
        record
        for record in IdempotencyLedger(store).list_for_task(task_id)
        if record.operation_type == "background.resume"
    ]
    assert len(records) == 1
    assert records[0].status is IdempotencyStatus.UNCERTAIN
    assert queue.get(task_id).state is TaskState.PAUSED
    assert resources.active_count(scope="background_life", owner_id="living-agent") == 0
    uncertain = [
        event
        for event in audit.list_for(entity_type="task", entity_id=task_id)
        if event.event_type == "background.resume_uncertain"
    ]
    assert len(uncertain) == 1
    assert uncertain[0].payload["error_type"] == "RuntimeError"

    async def replay() -> object:
        raise AssertionError("uncertain resume effect must not replay")

    with pytest.raises(IdempotencyConflictError, match="pending or uncertain"):
        asyncio.run(
            guard.resume_paused(
                task_id=task_id,
                work_kind=BackgroundWorkKind.UNFINISHED_WORK,
                effect=replay,
            )
        )


def test_resume_paused_rejects_unrelated_terminal_event_as_advancement(
    tmp_path: Path,
) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    store = _store(tmp_path)
    guard, queue, audit, resources = _guard(
        store=store,
        observations=[
            _obs(320, OwnerPresence.AWAY, now=now),
            _obs(321, OwnerPresence.AWAY, now=now),
            _obs(322, OwnerPresence.AWAY, now=now),
            _obs(323, OwnerPresence.AWAY, now=now),
            _obs(324, OwnerPresence.AWAY, now=now),
        ],
        now=now,
    )
    task_id, _pause_event_id = _owner_return_paused_task(queue, audit)

    async def unrelated_cancel() -> object:
        queue.transition(task_id, TaskState.CANCELLED)
        return "not-a-resume"

    with pytest.raises(RuntimeError, match="PAUSED -> READY -> RUNNING"):
        asyncio.run(
            guard.resume_paused(
                task_id=task_id,
                work_kind=BackgroundWorkKind.UNFINISHED_WORK,
                effect=unrelated_cancel,
            )
        )

    records = [
        record
        for record in IdempotencyLedger(store).list_for_task(task_id)
        if record.operation_type == "background.resume"
    ]
    assert len(records) == 1
    assert records[0].status is IdempotencyStatus.UNCERTAIN
    assert queue.get(task_id).state is TaskState.CANCELLED
    assert resources.active_count(scope="background_life", owner_id="living-agent") == 0
    assert not any(
        event.event_type == "background.resume_returned"
        for event in audit.list_for(entity_type="task", entity_id=task_id)
    )


def test_final_resource_fence_blocks_pressure_after_final_presence(
    tmp_path: Path,
) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    store = _store(tmp_path)
    resource_observer = SequenceResourceObserver(
        [10.0, 10.0, 10.0, 10.0, 10.0, 95.0]
    )
    guard, queue, audit, resources = _guard(
        store=store,
        observations=[
            _obs(400, OwnerPresence.AWAY, now=now),
            _obs(401, OwnerPresence.AWAY, now=now),
            _obs(402, OwnerPresence.AWAY, now=now),
            _obs(403, OwnerPresence.AWAY, now=now),
            _obs(404, OwnerPresence.AWAY, now=now),
        ],
        now=now,
        resource_observer=resource_observer,
    )
    task_id = _ready_task(queue)
    calls = 0

    async def effect() -> object:
        nonlocal calls
        calls += 1
        return "must-not-run"

    result = asyncio.run(
        guard.dispatch(
            task_id=task_id,
            work_kind=BackgroundWorkKind.READING_RESEARCH,
            effect=effect,
        )
    )

    assert result.action is BackgroundAction.DEFER
    assert result.reason == "resource_pressure"
    assert result.executed is False
    assert calls == 0
    assert queue.get(task_id).state is TaskState.READY
    assert resources.active_count(scope="background_life", owner_id="living-agent") == 0
    deferred = [
        event
        for event in audit.list_for(entity_type="task", entity_id=task_id)
        if event.event_type == "background.dispatch_deferred"
    ]
    assert deferred[-1].payload["phase"] == "effect_start_resource_fence"


def test_resume_final_resource_fence_blocks_power_change(
    tmp_path: Path,
) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    store = _store(tmp_path)
    resource_observer = SequenceResourceObserver(
        [10.0, 10.0, 10.0, 10.0, 10.0, 10.0],
        [True, True, True, True, True, False],
    )
    guard, queue, audit, resources = _guard(
        store=store,
        observations=[
            _obs(410, OwnerPresence.AWAY, now=now),
            _obs(411, OwnerPresence.AWAY, now=now),
            _obs(412, OwnerPresence.AWAY, now=now),
            _obs(413, OwnerPresence.AWAY, now=now),
            _obs(414, OwnerPresence.AWAY, now=now),
        ],
        now=now,
        resource_observer=resource_observer,
    )
    task_id, pause_event_id = _owner_return_paused_task(queue, audit)
    calls = 0

    async def effect() -> object:
        nonlocal calls
        calls += 1
        return "must-not-run"

    result = asyncio.run(
        guard.resume_paused(
            task_id=task_id,
            work_kind=BackgroundWorkKind.EVALUATION,
            effect=effect,
        )
    )

    assert result.action is BackgroundAction.DEFER
    assert result.reason == "battery_power"
    assert result.executed is False
    assert calls == 0
    assert queue.get(task_id).state is TaskState.PAUSED
    assert resources.active_count(scope="background_life", owner_id="living-agent") == 0
    blocked = [
        event
        for event in audit.list_for(entity_type="task", entity_id=task_id)
        if event.event_type == "background.resume_blocked"
    ]
    assert blocked[-1].payload["phase"] == "effect_start_resource_fence"
    assert blocked[-1].payload["pause_event_id"] == pause_event_id
