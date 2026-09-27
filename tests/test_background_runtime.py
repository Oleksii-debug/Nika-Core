from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone
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


class SequencePresence:
    def __init__(self, observations: list[OwnerPresenceObservation]) -> None:
        self._observations = iter(observations)

    def observe(self) -> OwnerPresenceObservation:
        return next(self._observations)


class SequenceResourceObserver:
    def __init__(self, cpu_values: list[float] | None = None) -> None:
        self._cpu_values = iter(cpu_values or [10.0] * 20)

    def snapshot(self) -> ResourceSnapshot:
        return ResourceSnapshot(
            cpu_percent=next(self._cpu_values),
            memory_percent=20.0,
            available_memory_bytes=2_000_000_000,
            power_plugged=True,
        )


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    return store


def _ready_task(queue: TaskQueue) -> str:
    task = queue.create(workspace_id="living", agent_id="nika")
    queue.transition(task.task_id, TaskState.READY)
    return task.task_id


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


def test_resource_pressure_race_at_final_admission_defers_without_effect(
    tmp_path: Path,
) -> None:
    now = datetime(2030, 1, 1, tzinfo=UTC)
    store = _store(tmp_path)
    resource_observer = SequenceResourceObserver([10.0, 10.0, 95.0])
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
