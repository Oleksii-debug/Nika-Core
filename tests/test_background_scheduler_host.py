from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nika_core.background_life import BackgroundWorkKind, OwnerPresence
from nika_core.background_runtime import BackgroundDispatchGuard, OwnerPresenceObservation
from nika_core.background_scheduler_host import BackgroundSchedulerHost
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.resources import ResourceBudget, ResourceManager, ResourceSnapshot
from nika_core.scheduler.recurrence import DurableRecurrenceService, RecurrenceStatus


@dataclass
class FakeClock:
    value: datetime

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: int) -> None:
        self.value += timedelta(**kwargs)


class MutablePresenceObserver:
    def __init__(
        self,
        clock: FakeClock,
        *,
        presence: OwnerPresence = OwnerPresence.AWAY,
        start_sequence: int = 0,
    ) -> None:
        self.clock = clock
        self.presence = presence
        self.sequence = start_sequence
        self.calls = 0

    def observe(self) -> OwnerPresenceObservation:
        self.calls += 1
        self.sequence += 1
        return OwnerPresenceObservation(
            source_id="host-presence",
            sequence=self.sequence,
            presence=self.presence,
            observed_at=self.clock.value,
        )


class MutableResourceObserver:
    def __init__(self) -> None:
        self.cpu_percent = 10.0
        self.memory_percent = 20.0

    def snapshot(self) -> ResourceSnapshot:
        return ResourceSnapshot(
            cpu_percent=self.cpu_percent,
            memory_percent=self.memory_percent,
            available_memory_bytes=2_000_000_000,
            power_plugged=True,
        )


class EffectRegistry:
    def __init__(self) -> None:
        self.effect_calls = 0
        self.resolve_calls: list[str] = []

    def resolve(self, action_id: str):
        self.resolve_calls.append(action_id)
        if action_id != "background.read":
            raise KeyError(action_id)

        async def effect() -> object:
            self.effect_calls += 1
            return {"ok": True}

        return effect


@dataclass
class Harness:
    store: SQLiteStore
    clock: FakeClock
    queue: TaskQueue
    task_id: str
    audit: AuditLog
    presence: MutablePresenceObserver
    resource_observer: MutableResourceObserver
    resources: ResourceManager
    effects: EffectRegistry
    host: BackgroundSchedulerHost


def _store(tmp_path: Path, name: str = "background scheduler host.db") -> SQLiteStore:
    store = SQLiteStore(tmp_path / name)
    store.initialize()
    return store


def _build_host(
    *,
    store: SQLiteStore,
    clock: FakeClock,
    effects: EffectRegistry,
    presence: MutablePresenceObserver | None = None,
    resource_observer: MutableResourceObserver | None = None,
) -> tuple[
    BackgroundSchedulerHost,
    AuditLog,
    MutablePresenceObserver,
    MutableResourceObserver,
    ResourceManager,
]:
    audit = AuditLog(store)
    observer = presence or MutablePresenceObserver(clock)
    resources_observer = resource_observer or MutableResourceObserver()
    resources = ResourceManager(store, resources_observer)
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
        queue=TaskQueue(store),
        audit=audit,
        resources=resources,
        presence=observer,
        source_id="host-presence",
        clock=clock,
    )
    host = BackgroundSchedulerHost(
        store=store,
        audit=audit,
        guard=guard,
        effect_resolver=effects.resolve,
        clock=clock,
    )
    return host, audit, observer, resources_observer, resources


def _harness(tmp_path: Path) -> Harness:
    store = _store(tmp_path)
    clock = FakeClock(datetime(2030, 1, 1, 12, 0, tzinfo=UTC))
    queue = TaskQueue(store)
    task = queue.create(workspace_id="living", agent_id="nika")
    queue.transition(task.task_id, TaskState.READY)
    effects = EffectRegistry()
    host, audit, presence, resource_observer, resources = _build_host(
        store=store,
        clock=clock,
        effects=effects,
    )
    return Harness(
        store=store,
        clock=clock,
        queue=queue,
        task_id=task.task_id,
        audit=audit,
        presence=presence,
        resource_observer=resource_observer,
        resources=resources,
        effects=effects,
        host=host,
    )


def _create(h: Harness, recurrence_id: str = "host-read") -> None:
    h.host.create(
        recurrence_id=recurrence_id,
        task_id=h.task_id,
        work_kind=BackgroundWorkKind.READING_RESEARCH,
        effect_action_id="background.read",
        interval_seconds=60,
        start_at=h.clock.value,
    )


def _only_enabled_job_id(host: BackgroundSchedulerHost) -> str:
    jobs = host._jobs.list_enabled()
    assert len(jobs) == 1
    return jobs[0].job_id


def test_start_installs_one_runtime_job_and_lifecycle_calls_are_idempotent(
    tmp_path: Path,
) -> None:
    h = _harness(tmp_path)
    _create(h)

    h.host.start()
    try:
        assert h.host.runtime_job_installed("host-read")
        h.host.start()
        assert h.host.runtime_job_installed("host-read")
    finally:
        h.host.shutdown()
        h.host.shutdown()

    assert not h.host.runtime_job_installed("host-read")


def test_fresh_host_reinstalls_same_durable_intent_after_restart(tmp_path: Path) -> None:
    h = _harness(tmp_path)
    _create(h)
    before = h.host.get("host-read")
    assert before is not None

    h.host.start()
    assert h.host.runtime_job_installed("host-read")
    h.host.shutdown()

    restarted, _, _, _, _ = _build_host(
        store=h.store,
        clock=h.clock,
        effects=h.effects,
    )
    restarted.start()
    try:
        assert restarted.runtime_job_installed("host-read")
        assert restarted.get("host-read") == before
    finally:
        restarted.shutdown()


def test_pause_resume_cancel_delegate_to_canonical_recurrence_and_scheduler(
    tmp_path: Path,
) -> None:
    h = _harness(tmp_path)
    h.host.start()
    try:
        _create(h)
        assert h.host.runtime_job_installed("host-read")

        paused = h.host.pause("host-read")
        assert paused.status is RecurrenceStatus.PAUSED
        assert not h.host.runtime_job_installed("host-read")

        resumed = h.host.resume("host-read")
        assert resumed.status is RecurrenceStatus.ACTIVE
        assert h.host.runtime_job_installed("host-read")

        cancelled = h.host.cancel("host-read")
        assert cancelled.status is RecurrenceStatus.CANCELLED
        assert not h.host.runtime_job_installed("host-read")
    finally:
        h.host.shutdown()


def test_actual_scheduler_route_runs_once_then_removes_future_intent(tmp_path: Path) -> None:
    h = _harness(tmp_path)
    h.host.start()
    try:
        _create(h)
        job_id = _only_enabled_job_id(h.host)
        assert h.host.runtime_job_installed("host-read")

        h.host._scheduler._dispatch(job_id)

        assert h.effects.resolve_calls == ["background.read"]
        assert h.effects.effect_calls == 1
        state = h.host.get("host-read")
        assert state is not None
        assert state.status is RecurrenceStatus.COMPLETED
        assert not h.host.runtime_job_installed("host-read")
    finally:
        h.host.shutdown()


def test_owner_active_keeps_one_future_intent_then_away_retry_completes(
    tmp_path: Path,
) -> None:
    h = _harness(tmp_path)
    h.presence.presence = OwnerPresence.ACTIVE
    h.host.start()
    try:
        _create(h)
        first_job_id = _only_enabled_job_id(h.host)
        h.host._scheduler._dispatch(first_job_id)

        waiting = h.host.get("host-read")
        assert waiting is not None
        assert waiting.status is RecurrenceStatus.ACTIVE
        assert waiting.next_due_at == h.clock.value + timedelta(minutes=1)
        assert h.queue.get(h.task_id).state is TaskState.PAUSED
        assert h.effects.resolve_calls == []
        assert h.host.runtime_job_installed("host-read")

        h.clock.advance(minutes=1)
        h.presence.presence = OwnerPresence.AWAY
        second_job_id = _only_enabled_job_id(h.host)
        h.host._scheduler._dispatch(second_job_id)

        assert h.effects.effect_calls == 1
        assert h.queue.get(h.task_id).state is TaskState.READY
        finished = h.host.get("host-read")
        assert finished is not None
        assert finished.status is RecurrenceStatus.COMPLETED
        assert not h.host.runtime_job_installed("host-read")
    finally:
        h.host.shutdown()


def test_resource_pressure_defers_without_effect_and_keeps_bounded_retry(
    tmp_path: Path,
) -> None:
    h = _harness(tmp_path)
    h.resource_observer.cpu_percent = 95.0
    h.host.start()
    try:
        _create(h)
        job_id = _only_enabled_job_id(h.host)
        h.host._scheduler._dispatch(job_id)

        waiting = h.host.get("host-read")
        assert waiting is not None
        assert waiting.status is RecurrenceStatus.ACTIVE
        assert waiting.next_due_at == h.clock.value + timedelta(minutes=1)
        assert h.effects.resolve_calls == []
        assert h.host.runtime_job_installed("host-read")

        h.clock.advance(minutes=1)
        h.resource_observer.cpu_percent = 10.0
        h.host._scheduler._dispatch(_only_enabled_job_id(h.host))

        assert h.effects.effect_calls == 1
        finished = h.host.get("host-read")
        assert finished is not None
        assert finished.status is RecurrenceStatus.COMPLETED
    finally:
        h.host.shutdown()


def test_terminal_parent_task_suppresses_durable_job_on_fresh_host_start(
    tmp_path: Path,
) -> None:
    h = _harness(tmp_path)
    _create(h)
    job_id = _only_enabled_job_id(h.host)
    h.queue.transition(h.task_id, TaskState.CANCELLED)

    restarted, audit, _, _, _ = _build_host(
        store=h.store,
        clock=h.clock,
        effects=h.effects,
    )
    restarted.start()
    try:
        assert not restarted.runtime_job_installed("host-read")
        assert restarted._jobs.list_enabled() == ()

        reconciled = restarted.get("host-read")
        assert reconciled is not None
        assert reconciled.status is RecurrenceStatus.CANCELLED
        assert reconciled.next_due_at is None
        assert reconciled.next_occurrence_id is None
        assert restarted.get("host-read") == reconciled

        events = audit.list_for(entity_type="scheduled_job", entity_id=job_id)
        suppressed = [
            event
            for event in events
            if event.event_type == "scheduler.job_suppressed_task_authority"
        ]
        assert len(suppressed) == 1
        assert suppressed[0].payload["reason"] == "terminal_task"
        assert suppressed[0].payload["task_state"] == TaskState.CANCELLED.value
    finally:
        restarted.shutdown()


def test_scheduler_route_rejects_unknown_or_noncanonical_action_before_handler(
    tmp_path: Path,
) -> None:
    h = _harness(tmp_path)

    class Text(str):
        pass

    with pytest.raises(KeyError, match="unknown scheduler action"):
        h.host._resolve_scheduler_action("scheduler.other")
    with pytest.raises(TypeError, match="scheduler action_id"):
        h.host._resolve_scheduler_action(Text(DurableRecurrenceService.ACTION_ID))
    assert h.presence.calls == 0
    assert h.effects.resolve_calls == []


def test_constructor_rejects_split_durable_or_audit_authority(tmp_path: Path) -> None:
    store = _store(tmp_path)
    other_store = _store(tmp_path, "other-background-host.db")
    clock = FakeClock(datetime(2030, 1, 1, 12, 0, tzinfo=UTC))
    audit = AuditLog(store)
    other_audit = AuditLog(store)
    presence = MutablePresenceObserver(clock)
    resource_observer = MutableResourceObserver()
    resources = ResourceManager(store, resource_observer)
    guard = BackgroundDispatchGuard(
        queue=TaskQueue(store),
        audit=audit,
        resources=resources,
        presence=presence,
        source_id="host-presence",
        clock=clock,
    )

    with pytest.raises(ValueError, match="share the exact AuditLog"):
        BackgroundSchedulerHost(
            store=store,
            audit=other_audit,
            guard=guard,
            effect_resolver=EffectRegistry().resolve,
            clock=clock,
        )

    split_guard = BackgroundDispatchGuard(
        queue=TaskQueue(other_store),
        audit=audit,
        resources=ResourceManager(other_store, MutableResourceObserver()),
        presence=presence,
        source_id="host-presence",
        clock=clock,
    )
    with pytest.raises(ValueError, match="TaskQueue"):
        BackgroundSchedulerHost(
            store=store,
            audit=audit,
            guard=split_guard,
            effect_resolver=EffectRegistry().resolve,
            clock=clock,
        )


def test_constructor_rejects_resource_manager_on_different_store(tmp_path: Path) -> None:
    store = _store(tmp_path)
    other_store = _store(tmp_path, "other-resources.db")
    clock = FakeClock(datetime(2030, 1, 1, 12, 0, tzinfo=UTC))
    audit = AuditLog(store)
    guard = BackgroundDispatchGuard(
        queue=TaskQueue(store),
        audit=audit,
        resources=ResourceManager(other_store, MutableResourceObserver()),
        presence=MutablePresenceObserver(clock),
        source_id="host-presence",
        clock=clock,
    )

    with pytest.raises(ValueError, match="ResourceManager"):
        BackgroundSchedulerHost(
            store=store,
            audit=audit,
            guard=guard,
            effect_resolver=EffectRegistry().resolve,
            clock=clock,
        )


def test_public_identity_and_shutdown_carriers_fail_closed(tmp_path: Path) -> None:
    h = _harness(tmp_path)

    class Text(str):
        pass

    with pytest.raises(TypeError, match="recurrence_id"):
        h.host.get(Text("host-read"))
    with pytest.raises(TypeError, match="wait"):
        h.host.shutdown(wait=1)
