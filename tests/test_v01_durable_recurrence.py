from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.scheduler.apscheduler_adapter import APSchedulerAdapter
from nika_core.scheduler.contracts import ScheduledJob
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


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "Nika recurrence тест.db")
    store.initialize()
    return store


def _service(
    store: SQLiteStore,
    clock: FakeClock,
    calls: list[RecurrenceInvocation],
    *,
    decision: RecurrenceDecision | None = None,
) -> tuple[DurableRecurrenceService, PersistingScheduler]:
    jobs = ScheduledJobStore(store)
    scheduler = PersistingScheduler(jobs)

    def resolve(action_id: str):
        assert action_id == "monitor.check"

        def handler(invocation: RecurrenceInvocation) -> RecurrenceDecision | None:
            calls.append(invocation)
            return decision

        return handler

    return (
        DurableRecurrenceService(
            jobs=jobs,
            scheduler=scheduler,
            handler_resolver=resolve,
            clock=clock,
        ),
        scheduler,
    )


def test_next_occurrence_is_durable_and_reconstructable_after_restart(tmp_path: Path) -> None:
    store = _store(tmp_path)
    clock = FakeClock(datetime(2030, 1, 1, 12, 0, tzinfo=UTC))
    calls: list[RecurrenceInvocation] = []
    service, scheduler = _service(store, clock, calls)
    start = clock.value + timedelta(minutes=5)

    created = service.create(
        recurrence_id="weather Київ",
        action_id="monitor.check",
        interval_seconds=300,
        start_at=start,
        payload={"query": "rain"},
        deadline_at=clock.value + timedelta(hours=1),
    )

    assert created.status is RecurrenceStatus.ACTIVE
    assert created.missed_run_policy is MissedRunPolicy.COALESCE_ONE
    assert created.next_due_at == start
    assert created.next_occurrence_id is not None
    assert scheduler.upserts[-1].trigger == {"run_date": start.isoformat()}
    assert scheduler.upserts[-1].misfire_grace_seconds is None

    restarted, _ = _service(store, clock, calls)
    reloaded = restarted.get("weather Київ")
    assert reloaded == created


def test_real_apscheduler_adapter_reinstalls_next_date_intent_without_sleep(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    jobs = ScheduledJobStore(store)
    clock = FakeClock(datetime(2030, 1, 1, 12, 0, tzinfo=UTC))
    calls: list[RecurrenceInvocation] = []
    service_ref: dict[str, DurableRecurrenceService] = {}

    def scheduler_resolver(action_id: str):
        assert action_id == DurableRecurrenceService.ACTION_ID
        return service_ref["service"].action_handler

    def target_resolver(action_id: str):
        assert action_id == "monitor.check"

        def handler(invocation: RecurrenceInvocation) -> None:
            calls.append(invocation)

        return handler

    adapter = APSchedulerAdapter(jobs, scheduler_resolver)
    service = DurableRecurrenceService(
        jobs=jobs,
        scheduler=adapter,
        handler_resolver=target_resolver,
        clock=clock,
    )
    service_ref["service"] = service
    service.create(
        recurrence_id="apscheduler-integration",
        action_id="monitor.check",
        interval_seconds=60,
        start_at=clock.value,
    )
    job_id = jobs.list_enabled()[0].job_id

    adapter.start()
    try:
        assert adapter.has_runtime_job(job_id)
        service.action_handler({"recurrence_id": "apscheduler-integration"})
        assert len(calls) == 1
        state = service.get("apscheduler-integration")
        assert state is not None
        assert state.next_due_at == clock.value + timedelta(minutes=1)
        assert adapter.has_runtime_job(job_id)
    finally:
        adapter.shutdown()


def test_completed_occurrence_is_not_repeated_and_missed_runs_coalesce_once(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    start = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    clock = FakeClock(start)
    calls: list[RecurrenceInvocation] = []
    service, _ = _service(store, clock, calls)
    service.create(
        recurrence_id="monitor",
        action_id="monitor.check",
        interval_seconds=300,
        start_at=start,
    )

    service.action_handler({"recurrence_id": "monitor"})
    first = calls[0]
    after_first = service.get("monitor")
    assert after_first is not None
    assert after_first.last_completed_occurrence_id == first.occurrence_id
    assert after_first.next_due_at == start + timedelta(minutes=5)
    assert after_first.next_occurrence_id != first.occurrence_id

    service.action_handler({"recurrence_id": "monitor"})
    assert calls == [first]

    clock.advance(minutes=22)
    restarted, _ = _service(store, clock, calls)
    overdue = restarted.get("monitor")
    assert overdue is not None
    assert overdue.next_due_at == start + timedelta(minutes=5)
    stable_overdue_id = overdue.next_occurrence_id

    restarted.action_handler({"recurrence_id": "monitor"})
    assert len(calls) == 2
    assert calls[-1].scheduled_for == start + timedelta(minutes=5)
    assert calls[-1].occurrence_id == stable_overdue_id
    after_catchup = restarted.get("monitor")
    assert after_catchup is not None
    assert after_catchup.next_due_at == start + timedelta(minutes=25)

    restarted.action_handler({"recurrence_id": "monitor"})
    assert len(calls) == 2


def test_pause_survives_restart_and_resume_keeps_one_coalesced_intent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = datetime(2030, 1, 1, 12, 5, tzinfo=UTC)
    clock = FakeClock(start - timedelta(minutes=5))
    calls: list[RecurrenceInvocation] = []
    service, _ = _service(store, clock, calls)
    service.create(
        recurrence_id="paused-monitor",
        action_id="monitor.check",
        interval_seconds=300,
        start_at=start,
    )
    paused = service.pause("paused-monitor")
    assert paused.status is RecurrenceStatus.PAUSED

    clock.advance(minutes=16)
    restarted, _ = _service(store, clock, calls)
    assert restarted.get("paused-monitor") == paused
    restarted.action_handler({"recurrence_id": "paused-monitor"})
    assert calls == []

    resumed = restarted.resume("paused-monitor")
    assert resumed.status is RecurrenceStatus.ACTIVE
    assert resumed.next_due_at == start
    restarted.action_handler({"recurrence_id": "paused-monitor"})
    assert len(calls) == 1
    assert calls[0].scheduled_for == start

    next_state = restarted.get("paused-monitor")
    assert next_state is not None
    assert next_state.next_due_at == datetime(2030, 1, 1, 12, 20, tzinfo=UTC)
    restarted.action_handler({"recurrence_id": "paused-monitor"})
    assert len(calls) == 1


def test_cancel_is_durable_idempotent_and_terminates_recurrence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    clock = FakeClock(datetime(2030, 1, 1, 12, 0, tzinfo=UTC))
    calls: list[RecurrenceInvocation] = []
    service, _ = _service(store, clock, calls)
    service.create(
        recurrence_id="cancel-me",
        action_id="monitor.check",
        interval_seconds=60,
        start_at=clock.value + timedelta(minutes=1),
    )

    cancelled = service.cancel("cancel-me")
    assert cancelled.status is RecurrenceStatus.CANCELLED
    assert service.cancel("cancel-me") == cancelled
    clock.advance(minutes=30)

    restarted, _ = _service(store, clock, calls)
    assert restarted.get("cancel-me") == cancelled
    restarted.action_handler({"recurrence_id": "cancel-me"})
    assert calls == []


def test_deadline_stops_future_occurrences_without_late_catchup(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    clock = FakeClock(start)
    calls: list[RecurrenceInvocation] = []
    service, _ = _service(store, clock, calls)
    service.create(
        recurrence_id="deadline-monitor",
        action_id="monitor.check",
        interval_seconds=300,
        start_at=start,
        deadline_at=start + timedelta(minutes=12),
    )

    service.action_handler({"recurrence_id": "deadline-monitor"})
    clock.advance(minutes=5)
    service.action_handler({"recurrence_id": "deadline-monitor"})
    clock.advance(minutes=5)
    service.action_handler({"recurrence_id": "deadline-monitor"})

    state = service.get("deadline-monitor")
    assert state is not None
    assert state.status is RecurrenceStatus.COMPLETED
    assert state.terminal_reason is RecurrenceTerminalReason.DEADLINE
    assert state.next_due_at is None
    assert len(calls) == 3

    clock.advance(hours=1)
    service.action_handler({"recurrence_id": "deadline-monitor"})
    assert len(calls) == 3


def test_restart_after_deadline_terminates_overdue_intent_without_handler(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    clock = FakeClock(start)
    calls: list[RecurrenceInvocation] = []
    service, _ = _service(store, clock, calls)
    service.create(
        recurrence_id="offline-deadline",
        action_id="monitor.check",
        interval_seconds=300,
        start_at=start + timedelta(minutes=5),
        deadline_at=start + timedelta(minutes=10),
    )

    clock.advance(minutes=20)
    restarted, _ = _service(store, clock, calls)
    restarted.action_handler({"recurrence_id": "offline-deadline"})
    state = restarted.get("offline-deadline")
    assert state is not None
    assert state.status is RecurrenceStatus.COMPLETED
    assert state.terminal_reason is RecurrenceTerminalReason.DEADLINE
    assert calls == []


def test_condition_stop_terminates_recurrence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    clock = FakeClock(start)
    calls: list[RecurrenceInvocation] = []
    service, _ = _service(store, clock, calls, decision=RecurrenceDecision.STOP)
    service.create(
        recurrence_id="until-condition",
        action_id="monitor.check",
        interval_seconds=60,
        start_at=start,
    )

    service.action_handler({"recurrence_id": "until-condition"})
    state = service.get("until-condition")
    assert state is not None
    assert state.status is RecurrenceStatus.COMPLETED
    assert state.terminal_reason is RecurrenceTerminalReason.CONDITION_MET
    assert state.next_due_at is None
    assert len(calls) == 1


def test_occurrence_identity_is_stable_for_external_effect_dedupe(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    clock = FakeClock(start)
    calls: list[RecurrenceInvocation] = []
    service, _ = _service(store, clock, calls)
    initial = service.create(
        recurrence_id="effect-series",
        action_id="monitor.check",
        interval_seconds=60,
        start_at=start,
    )
    assert initial.next_occurrence_id is not None

    restarted, _ = _service(store, clock, calls)
    before_run = restarted.get("effect-series")
    assert before_run is not None
    assert before_run.next_occurrence_id == initial.next_occurrence_id
    restarted.action_handler({"recurrence_id": "effect-series"})
    assert calls[0].occurrence_id == initial.next_occurrence_id

    next_state = restarted.get("effect-series")
    assert next_state is not None
    assert next_state.next_occurrence_id is not None
    assert next_state.next_occurrence_id != initial.next_occurrence_id


def test_same_recurrence_id_with_conflicting_definition_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    clock = FakeClock(datetime(2030, 1, 1, 12, 0, tzinfo=UTC))
    calls: list[RecurrenceInvocation] = []
    service, _ = _service(store, clock, calls)
    service.create(
        recurrence_id="same-id",
        action_id="monitor.check",
        interval_seconds=60,
        start_at=clock.value,
        payload={"scope": "one"},
    )

    with pytest.raises(ValueError, match="different recurrence"):
        service.create(
            recurrence_id="same-id",
            action_id="monitor.check",
            interval_seconds=120,
            start_at=clock.value,
            payload={"scope": "one"},
        )


def test_naive_time_and_invalid_interval_fail_before_persistence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    clock = FakeClock(datetime(2030, 1, 1, 12, 0, tzinfo=UTC))
    calls: list[RecurrenceInvocation] = []
    service, _ = _service(store, clock, calls)

    with pytest.raises(ValueError, match="timezone-aware"):
        service.create(
            recurrence_id="naive",
            action_id="monitor.check",
            interval_seconds=60,
            start_at=datetime(2030, 1, 1, 12, 0),
        )
    with pytest.raises(ValueError, match="positive integer"):
        service.create(
            recurrence_id="bad-interval",
            action_id="monitor.check",
            interval_seconds=0,
            start_at=clock.value,
        )
    assert service.get("naive") is None
    assert service.get("bad-interval") is None
