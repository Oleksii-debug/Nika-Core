from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.checkpoint import CheckpointService
from nika_core.kernel.task_queue import TaskQueue
from nika_core.kernel.task_state import TaskState
from nika_core.research.models import (
    RefreshDisposition,
    RefreshResult,
    SourceKind,
    SourceSpec,
)
from nika_core.research.v01_monitoring import (
    MonitorCondition,
    NormalizedObservation,
    V01MonitoringLoop,
)
from nika_core.scheduler.contracts import ScheduledJob, TriggerKind
from nika_core.scheduler.recurrence import (
    DurableRecurrenceService,
    MissedRunPolicy,
    RecurrenceDecision,
    RecurrenceInvocation,
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


class FakeNetworkRepository:
    def __init__(self) -> None:
        self.sources: dict[str, SimpleNamespace] = {}

    def get_source(self, source_id: str) -> SimpleNamespace:
        try:
            return self.sources[source_id]
        except KeyError:
            raise KeyError(source_id) from None


class FakeWebService:
    def __init__(
        self,
        network: FakeNetworkRepository,
        observations: list[NormalizedObservation],
    ) -> None:
        self.network = network
        self.observations = list(observations)
        self.current: NormalizedObservation | None = None
        self.refresh_calls = 0

    def register_source(self, source: SourceSpec) -> None:
        self.network.sources[source.source_id] = SimpleNamespace(
            workspace_id=source.workspace_id,
            url=source.locator,
        )

    def refresh_source(self, source_id: str, *, task_id: str) -> RefreshResult:
        del task_id
        self.refresh_calls += 1
        if not self.observations:
            raise AssertionError("test web service received an unexpected fetch")
        current = self.observations.pop(0)
        assert current.source_id == source_id
        previous = self.current
        self.current = current
        disposition = (
            RefreshDisposition.CHANGED
            if previous is None or previous.normalized_sha256 != current.normalized_sha256
            else RefreshDisposition.UNCHANGED
        )
        return RefreshResult(
            source_id=source_id,
            disposition=disposition,
            attempts=1,
            snapshot_id=current.snapshot_id,
            document_id=current.document_id,
        )


class HarnessMonitoringLoop(V01MonitoringLoop):
    def __init__(self, *, fake_web: FakeWebService, **kwargs: Any) -> None:
        self._fake_web = fake_web
        super().__init__(web=fake_web, **kwargs)

    def _current_observation(self, config: object) -> NormalizedObservation | None:
        del config
        return self._fake_web.current


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "Nika monitor recurrence тест.db")
    store.initialize()
    return store


def _observation(number: int, digest: str) -> NormalizedObservation:
    return NormalizedObservation(
        source_id="source-1",
        workspace_id="workspace-1",
        declared_locator="https://example.test/status",
        resolved_locator="https://example.test/status",
        snapshot_id=f"snapshot-{number}",
        document_id=f"document-{number}",
        normalized_sha256=digest,
        source_observed_at=f"2030-01-01T12:{number:02d}:00+00:00",
    )


def _runtime(
    store: SQLiteStore,
    clock: FakeClock,
    network: FakeNetworkRepository,
    web: FakeWebService,
) -> tuple[HarnessMonitoringLoop, DurableRecurrenceService, PersistingScheduler]:
    jobs = ScheduledJobStore(store)
    scheduler = PersistingScheduler(jobs)
    loop_ref: dict[str, HarnessMonitoringLoop] = {}

    def resolve(action_id: str):
        assert action_id == V01MonitoringLoop.ACTION_ID
        return loop_ref["loop"].handle_occurrence

    recurrence = DurableRecurrenceService(
        jobs=jobs,
        scheduler=scheduler,
        handler_resolver=resolve,
        clock=clock,
    )
    loop = HarnessMonitoringLoop(
        store=store,
        network_repository=network,
        fake_web=web,
        tasks=TaskQueue(store),
        checkpoints=CheckpointService(store),
        recurrence=recurrence,
        clock=clock,
    )
    loop_ref["loop"] = loop
    return loop, recurrence, scheduler


def _source() -> SourceSpec:
    return SourceSpec(
        source_id="source-1",
        workspace_id="workspace-1",
        kind=SourceKind.HTTP,
        locator="https://example.test/status",
    )


def _invocation(
    recurrence: DurableRecurrenceService,
    task_id: str,
) -> RecurrenceInvocation:
    recurrence_id = V01MonitoringLoop.recurrence_id(task_id)
    state = recurrence.get(recurrence_id)
    assert state is not None
    assert state.next_occurrence_id is not None
    assert state.next_due_at is not None
    return RecurrenceInvocation(
        recurrence_id=recurrence_id,
        occurrence_id=state.next_occurrence_id,
        scheduled_for=state.next_due_at,
        payload={"monitor_task_id": task_id},
    )


def test_occurrence_checkpoint_survives_restart_without_duplicate_fetch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    clock = FakeClock(datetime(2030, 1, 1, 12, 0, tzinfo=UTC))
    network = FakeNetworkRepository()
    web = FakeWebService(network, [_observation(1, "aaa")])
    loop, recurrence, scheduler = _runtime(store, clock, network, web)

    handle = loop.start(_source(), interval_seconds=300)
    state = recurrence.get(handle.recurrence_id)
    assert state is not None
    assert state.missed_run_policy is MissedRunPolicy.COALESCE_ONE
    assert scheduler.upserts[-1].trigger_kind is TriggerKind.DATE
    assert scheduler.upserts[-1].action_id == DurableRecurrenceService.ACTION_ID
    assert TaskQueue(store).get(handle.task_id).payload.keys() >= {
        "source_id",
        "workspace_id",
        "locator",
        "condition",
    }
    assert "interval_seconds" not in TaskQueue(store).get(handle.task_id).payload
    invocation = _invocation(recurrence, handle.task_id)

    result = loop.handle_occurrence(invocation)
    assert result is RecurrenceDecision.CONTINUE
    checkpoint = CheckpointService(store).latest(handle.task_id)
    assert checkpoint is not None
    assert checkpoint.payload["occurrence_id"] == invocation.occurrence_id
    assert web.refresh_calls == 1

    _, restarted_recurrence, _ = _runtime(store, clock, network, web)
    restarted_recurrence.action_handler({"recurrence_id": handle.recurrence_id})

    assert web.refresh_calls == 1
    after = restarted_recurrence.get(handle.recurrence_id)
    assert after is not None
    assert after.last_completed_occurrence_id == invocation.occurrence_id
    assert after.next_due_at == clock.value + timedelta(minutes=5)


def test_condition_checkpoint_replay_stops_recurrence_without_refetch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    clock = FakeClock(datetime(2030, 1, 1, 12, 0, tzinfo=UTC))
    network = FakeNetworkRepository()
    web = FakeWebService(
        network,
        [
            _observation(1, "aaa"),
            _observation(2, "bbb"),
        ],
    )
    loop, recurrence, _ = _runtime(store, clock, network, web)
    handle = loop.start(
        _source(),
        interval_seconds=300,
        condition=MonitorCondition.CHANGED,
    )

    recurrence.action_handler({"recurrence_id": handle.recurrence_id})
    clock.advance(minutes=5)
    invocation = _invocation(recurrence, handle.task_id)

    decision = loop.handle_occurrence(invocation)
    assert decision is RecurrenceDecision.STOP
    assert web.refresh_calls == 2
    assert TaskQueue(store).get(handle.task_id).state is TaskState.COMPLETED

    _, restarted_recurrence, _ = _runtime(store, clock, network, web)
    restarted_recurrence.action_handler({"recurrence_id": handle.recurrence_id})

    assert web.refresh_calls == 2
    terminal = restarted_recurrence.get(handle.recurrence_id)
    assert terminal is not None
    assert terminal.status is RecurrenceStatus.COMPLETED
    assert terminal.terminal_reason is RecurrenceTerminalReason.CONDITION_MET
    assert terminal.last_completed_occurrence_id == invocation.occurrence_id


def test_missed_occurrences_use_canonical_coalesce_one_without_catchup_storm(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    start = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    clock = FakeClock(start)
    network = FakeNetworkRepository()
    web = FakeWebService(
        network,
        [
            _observation(1, "aaa"),
            _observation(2, "aaa"),
        ],
    )
    loop, recurrence, _ = _runtime(store, clock, network, web)
    handle = loop.start(_source(), interval_seconds=300)

    recurrence.action_handler({"recurrence_id": handle.recurrence_id})
    clock.advance(minutes=22)
    _, restarted_recurrence, _ = _runtime(store, clock, network, web)
    restarted_recurrence.action_handler({"recurrence_id": handle.recurrence_id})

    assert web.refresh_calls == 2
    state = restarted_recurrence.get(handle.recurrence_id)
    assert state is not None
    assert state.next_due_at == start + timedelta(minutes=25)
    restarted_recurrence.action_handler({"recurrence_id": handle.recurrence_id})
    assert web.refresh_calls == 2


def test_durable_pause_guard_prevents_fetch_after_crash_before_recurrence_pause(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    clock = FakeClock(datetime(2030, 1, 1, 12, 0, tzinfo=UTC))
    network = FakeNetworkRepository()
    web = FakeWebService(network, [_observation(1, "aaa")])
    loop, recurrence, _ = _runtime(store, clock, network, web)
    handle = loop.start(_source(), interval_seconds=300)

    TaskQueue(store).transition(handle.task_id, TaskState.PAUSED)
    recurrence.action_handler({"recurrence_id": handle.recurrence_id})

    assert web.refresh_calls == 0
    state = recurrence.get(handle.recurrence_id)
    assert state is not None
    assert state.status is RecurrenceStatus.PAUSED

    _, restarted_recurrence, _ = _runtime(store, clock, network, web)
    restarted_recurrence.action_handler({"recurrence_id": handle.recurrence_id})
    assert web.refresh_calls == 0


def test_durable_cancel_guard_prevents_fetch_after_crash_before_recurrence_cancel(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    clock = FakeClock(datetime(2030, 1, 1, 12, 0, tzinfo=UTC))
    network = FakeNetworkRepository()
    web = FakeWebService(network, [_observation(1, "aaa")])
    loop, recurrence, _ = _runtime(store, clock, network, web)
    handle = loop.start(_source(), interval_seconds=300)

    TaskQueue(store).transition(handle.task_id, TaskState.CANCELLED)
    recurrence.action_handler({"recurrence_id": handle.recurrence_id})

    assert web.refresh_calls == 0
    state = recurrence.get(handle.recurrence_id)
    assert state is not None
    assert state.status is RecurrenceStatus.CANCELLED


def test_deadline_is_checked_by_recurrence_before_monitor_fetch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    clock = FakeClock(start)
    network = FakeNetworkRepository()
    web = FakeWebService(network, [_observation(1, "aaa")])
    loop, recurrence, _ = _runtime(store, clock, network, web)
    handle = loop.start(
        _source(),
        interval_seconds=300,
        deadline_at=start + timedelta(minutes=2),
    )

    clock.advance(minutes=3)
    recurrence.action_handler({"recurrence_id": handle.recurrence_id})

    assert web.refresh_calls == 0
    state = recurrence.get(handle.recurrence_id)
    assert state is not None
    assert state.status is RecurrenceStatus.COMPLETED
    assert state.terminal_reason is RecurrenceTerminalReason.DEADLINE
    assert state.next_due_at is None


def test_paused_monitor_resume_after_deadline_completes_without_fetch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    clock = FakeClock(start)
    network = FakeNetworkRepository()
    web = FakeWebService(network, [_observation(1, "aaa")])
    loop, recurrence, _ = _runtime(store, clock, network, web)
    handle = loop.start(
        _source(),
        interval_seconds=300,
        deadline_at=start + timedelta(minutes=2),
    )
    loop.pause(handle.task_id)
    clock.advance(minutes=3)

    state = loop.resume(handle.task_id)

    assert state is TaskState.COMPLETED
    assert web.refresh_calls == 0
    recurrence_state = recurrence.get(handle.recurrence_id)
    assert recurrence_state is not None
    assert recurrence_state.status is RecurrenceStatus.COMPLETED
    assert recurrence_state.terminal_reason is RecurrenceTerminalReason.DEADLINE
