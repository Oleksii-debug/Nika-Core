from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nika_core.background_life import BackgroundWorkKind, OwnerPresence
from nika_core.background_recurrence import (
    BackgroundRecurrenceBinding,
    BackgroundRecurrenceBridge,
)
from nika_core.background_runtime import BackgroundDispatchGuard, OwnerPresenceObservation
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.resources import ResourceBudget, ResourceManager, ResourceSnapshot
from nika_core.runtime.idempotency import IdempotencyConflictError
from nika_core.scheduler.contracts import ScheduledJob
from nika_core.scheduler.recurrence import (
    DurableRecurrenceService,
    RecurrenceStatus,
    RecurrenceTerminalReason,
)
from nika_core.scheduler.store import ScheduledJobStore


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
            source_id="test-background-presence",
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


class PersistingScheduler:
    def __init__(self, jobs: ScheduledJobStore) -> None:
        self.jobs = jobs
        self.upserts: list[ScheduledJob] = []

    def start(self) -> None:
        return None

    def shutdown(self, *, wait: bool = True) -> None:
        del wait

    def upsert(self, job: ScheduledJob) -> None:
        self.jobs.upsert(job)
        self.upserts.append(job)

    def remove(self, job_id: str) -> bool:
        return self.jobs.delete(job_id)

    def pause(self, job_id: str) -> None:
        self.jobs.set_enabled(job_id, False)

    def resume(self, job_id: str) -> None:
        self.jobs.set_enabled(job_id, True)


class EffectRegistry:
    def __init__(self) -> None:
        self.resolve_calls: list[str] = []
        self.effect_calls = 0
        self.fail = False

    def resolve(self, action_id: str):
        self.resolve_calls.append(action_id)
        if action_id != "background.read":
            raise KeyError(action_id)

        async def effect() -> object:
            self.effect_calls += 1
            if self.fail:
                raise RuntimeError("synthetic effect failure")
            return {"ok": True}

        return effect


@dataclass
class Harness:
    store: SQLiteStore
    clock: FakeClock
    queue: TaskQueue
    task_id: str
    presence: MutablePresenceObserver
    resources_observer: MutableResourceObserver
    guard: BackgroundDispatchGuard
    effects: EffectRegistry
    bridge: BackgroundRecurrenceBridge
    jobs: ScheduledJobStore
    scheduler: PersistingScheduler
    recurrence: DurableRecurrenceService


def _harness(tmp_path: Path) -> Harness:
    store = SQLiteStore(tmp_path / "background-recurrence.db")
    store.initialize()
    clock = FakeClock(datetime(2030, 1, 1, 12, 0, tzinfo=UTC))
    queue = TaskQueue(store)
    task = queue.create(workspace_id="living", agent_id="nika")
    queue.transition(task.task_id, TaskState.READY)

    audit = AuditLog(store)
    presence = MutablePresenceObserver(clock)
    resources_observer = MutableResourceObserver()
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
        queue=queue,
        audit=audit,
        resources=resources,
        presence=presence,
        source_id="test-background-presence",
        clock=clock,
    )
    effects = EffectRegistry()
    bridge = BackgroundRecurrenceBridge(guard=guard, effect_resolver=effects.resolve)
    jobs = ScheduledJobStore(store)
    scheduler = PersistingScheduler(jobs)
    recurrence = DurableRecurrenceService(
        jobs=jobs,
        scheduler=scheduler,
        handler_resolver=bridge.resolve,
        clock=clock,
    )
    return Harness(
        store=store,
        clock=clock,
        queue=queue,
        task_id=task.task_id,
        presence=presence,
        resources_observer=resources_observer,
        guard=guard,
        effects=effects,
        bridge=bridge,
        jobs=jobs,
        scheduler=scheduler,
        recurrence=recurrence,
    )


def _create(h: Harness, *, recurrence_id: str = "living-read") -> None:
    h.bridge.create(
        h.recurrence,
        recurrence_id=recurrence_id,
        task_id=h.task_id,
        work_kind=BackgroundWorkKind.READING_RESEARCH,
        effect_action_id="background.read",
        interval_seconds=60,
        start_at=h.clock.value,
    )


def test_binding_round_trip_and_exact_carrier_fences() -> None:
    binding = BackgroundRecurrenceBinding(
        recurrence_id="r",
        task_id="t",
        work_kind=BackgroundWorkKind.READING_RESEARCH,
        effect_action_id="background.read",
    )
    assert BackgroundRecurrenceBinding.from_payload(binding.to_payload()) == binding

    class Text(str):
        pass

    with pytest.raises(TypeError, match="recurrence_id"):
        BackgroundRecurrenceBinding(
            recurrence_id=Text("r"),
            task_id="t",
            work_kind=BackgroundWorkKind.READING_RESEARCH,
            effect_action_id="background.read",
        )
    hostile = binding.to_payload()
    hostile["work_kind"] = Text(BackgroundWorkKind.READING_RESEARCH.value)
    with pytest.raises(TypeError, match="work_kind"):
        BackgroundRecurrenceBinding.from_payload(hostile)
    extra = binding.to_payload()
    extra["unexpected"] = True
    with pytest.raises(ValueError, match="unexpected fields"):
        BackgroundRecurrenceBinding.from_payload(extra)


def test_create_rejects_missing_task_before_scheduler_persistence(tmp_path: Path) -> None:
    h = _harness(tmp_path)

    with pytest.raises(KeyError):
        h.bridge.create(
            h.recurrence,
            recurrence_id="missing-task",
            task_id="does-not-exist",
            work_kind=BackgroundWorkKind.READING_RESEARCH,
            effect_action_id="background.read",
            interval_seconds=60,
            start_at=h.clock.value,
        )

    assert h.recurrence.get("missing-task") is None
    assert h.scheduler.upserts == []


@pytest.mark.parametrize("terminal_state", [TaskState.COMPLETED, TaskState.CANCELLED, TaskState.ARCHIVED])
def test_create_rejects_irreversibly_terminal_task(
    tmp_path: Path,
    terminal_state: TaskState,
) -> None:
    h = _harness(tmp_path)
    if terminal_state is TaskState.CANCELLED:
        h.queue.transition(h.task_id, TaskState.CANCELLED)
    else:
        h.queue.transition(h.task_id, TaskState.RUNNING)
        h.queue.transition(h.task_id, TaskState.COMPLETED)
        if terminal_state is TaskState.ARCHIVED:
            h.queue.transition(h.task_id, TaskState.ARCHIVED)

    with pytest.raises(ValueError, match="irreversibly terminal"):
        _create(h, recurrence_id="terminal-task")

    assert h.recurrence.get("terminal-task") is None


def test_create_binds_existing_task_work_kind_and_effect_immutably(tmp_path: Path) -> None:
    h = _harness(tmp_path)
    _create(h)

    state = h.recurrence.get("living-read")
    assert state is not None
    assert state.task_id == h.task_id
    assert state.action_id == BackgroundRecurrenceBridge.ACTION_ID
    with pytest.raises(ValueError, match="different recurrence"):
        h.bridge.create(
            h.recurrence,
            recurrence_id="living-read",
            task_id=h.task_id,
            work_kind=BackgroundWorkKind.SELF_TEST,
            effect_action_id="background.read",
            interval_seconds=60,
            start_at=h.clock.value,
        )


def test_away_success_executes_once_and_terminates_recurrence(tmp_path: Path) -> None:
    h = _harness(tmp_path)
    _create(h)

    h.recurrence.action_handler({"recurrence_id": "living-read"})

    assert h.effects.resolve_calls == ["background.read"]
    assert h.effects.effect_calls == 1
    state = h.recurrence.get("living-read")
    assert state is not None
    assert state.status is RecurrenceStatus.COMPLETED
    assert state.terminal_reason is RecurrenceTerminalReason.CONDITION_MET
    assert state.next_occurrence_id is None

    h.recurrence.action_handler({"recurrence_id": "living-read"})
    assert h.effects.effect_calls == 1


def test_owner_active_pauses_without_resolving_effect_then_away_retry_runs(
    tmp_path: Path,
) -> None:
    h = _harness(tmp_path)
    h.presence.presence = OwnerPresence.ACTIVE
    _create(h)

    h.recurrence.action_handler({"recurrence_id": "living-read"})

    assert h.effects.resolve_calls == []
    assert h.effects.effect_calls == 0
    assert h.queue.get(h.task_id).state is TaskState.PAUSED
    waiting = h.recurrence.get("living-read")
    assert waiting is not None
    assert waiting.status is RecurrenceStatus.ACTIVE
    assert waiting.next_due_at == h.clock.value + timedelta(minutes=1)
    stable_next_id = waiting.next_occurrence_id

    restarted_scheduler = PersistingScheduler(h.jobs)
    restarted = DurableRecurrenceService(
        jobs=h.jobs,
        scheduler=restarted_scheduler,
        handler_resolver=h.bridge.resolve,
        clock=h.clock,
    )
    assert restarted.get("living-read") == waiting
    reloaded = restarted.get("living-read")
    assert reloaded is not None
    assert reloaded.next_occurrence_id == stable_next_id

    h.clock.advance(minutes=1)
    h.presence.presence = OwnerPresence.AWAY
    restarted.action_handler({"recurrence_id": "living-read"})

    assert h.effects.effect_calls == 1
    assert h.queue.get(h.task_id).state is TaskState.READY
    finished = restarted.get("living-read")
    assert finished is not None
    assert finished.status is RecurrenceStatus.COMPLETED


def test_resource_pressure_defers_without_effect_then_retries_next_slot(tmp_path: Path) -> None:
    h = _harness(tmp_path)
    h.resources_observer.cpu_percent = 95.0
    _create(h)

    h.recurrence.action_handler({"recurrence_id": "living-read"})

    assert h.effects.resolve_calls == []
    waiting = h.recurrence.get("living-read")
    assert waiting is not None
    assert waiting.status is RecurrenceStatus.ACTIVE
    assert waiting.next_due_at == h.clock.value + timedelta(minutes=1)

    h.clock.advance(minutes=1)
    h.resources_observer.cpu_percent = 10.0
    h.recurrence.action_handler({"recurrence_id": "living-read"})
    assert h.effects.effect_calls == 1
    finished = h.recurrence.get("living-read")
    assert finished is not None
    assert finished.status is RecurrenceStatus.COMPLETED


@pytest.mark.parametrize(
    "terminal_state",
    [TaskState.COMPLETED, TaskState.CANCELLED, TaskState.ARCHIVED],
)
def test_terminal_bound_task_stops_recurrence_without_presence_or_effect(
    tmp_path: Path,
    terminal_state: TaskState,
) -> None:
    h = _harness(tmp_path)
    _create(h)
    if terminal_state is TaskState.CANCELLED:
        h.queue.transition(h.task_id, TaskState.CANCELLED)
    else:
        h.queue.transition(h.task_id, TaskState.RUNNING)
        h.queue.transition(h.task_id, TaskState.COMPLETED)
        if terminal_state is TaskState.ARCHIVED:
            h.queue.transition(h.task_id, TaskState.ARCHIVED)

    h.recurrence.action_handler({"recurrence_id": "living-read"})

    assert h.presence.calls == 0
    assert h.effects.resolve_calls == []
    assert h.effects.effect_calls == 0
    state = h.recurrence.get("living-read")
    assert state is not None
    assert state.status is RecurrenceStatus.COMPLETED
    assert state.terminal_reason is RecurrenceTerminalReason.CONDITION_MET
    assert state.next_occurrence_id is None


def test_effect_failure_leaves_occurrence_unadvanced_and_uncertain_blocks_replay(
    tmp_path: Path,
) -> None:
    h = _harness(tmp_path)
    h.effects.fail = True
    _create(h)
    before = h.recurrence.get("living-read")
    assert before is not None

    with pytest.raises(RuntimeError, match="synthetic effect failure"):
        h.recurrence.action_handler({"recurrence_id": "living-read"})

    after = h.recurrence.get("living-read")
    assert after == before
    assert h.effects.effect_calls == 1

    h.effects.fail = False
    with pytest.raises(IdempotencyConflictError, match="reconcile"):
        h.recurrence.action_handler({"recurrence_id": "living-read"})
    assert h.effects.effect_calls == 1
    assert h.recurrence.get("living-read") == before


def test_no_public_direct_invocation_authority_is_exposed(tmp_path: Path) -> None:
    h = _harness(tmp_path)

    assert not hasattr(h.bridge, "dispatch_invocation")
    assert not hasattr(h.bridge, "occurrence_handler")
    handler = h.bridge.resolve(BackgroundRecurrenceBridge.ACTION_ID)
    assert handler.__name__ == "_occurrence_handler"


def test_unknown_durable_recurrence_fails_before_presence_or_effect(tmp_path: Path) -> None:
    h = _harness(tmp_path)

    with pytest.raises(KeyError, match="unknown recurrence"):
        h.recurrence.action_handler({"recurrence_id": "forged"})

    assert h.presence.calls == 0
    assert h.effects.resolve_calls == []
    assert h.effects.effect_calls == 0


def test_resolver_is_only_touched_inside_permitted_effect_window(tmp_path: Path) -> None:
    h = _harness(tmp_path)
    h.presence.presence = OwnerPresence.UNKNOWN
    _create(h)

    h.recurrence.action_handler({"recurrence_id": "living-read"})

    assert h.effects.resolve_calls == []
    assert h.effects.effect_calls == 0


def test_bridge_rejects_unknown_action_and_noncanonical_guard() -> None:
    class Dummy:
        pass

    with pytest.raises(TypeError, match="guard"):
        BackgroundRecurrenceBridge(guard=Dummy(), effect_resolver=lambda _key: None)

    assert BackgroundRecurrenceBridge.ACTION_ID == "living.background.dispatch"

