from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from pathlib import Path

import pytest

import nika_core.scheduler.recurrence as recurrence_module
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_state import TaskState
from nika_core.scheduler.apscheduler_adapter import APSchedulerAdapter
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

TASK_ID = "task-recurrence-test"


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


def _set_task_state(store: SQLiteStore, task_id: str, state: TaskState) -> None:
    now = datetime.now(UTC).isoformat()
    with store.connection() as conn:
        existing = conn.execute(
            "SELECT 1 FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO tasks(
                    task_id, workspace_id, agent_id, state, payload_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    "recurrence-test-workspace",
                    "recurrence-test-agent",
                    state.value,
                    "{}",
                    now,
                    now,
                ),
            )
        else:
            conn.execute(
                "UPDATE tasks SET state = ?, updated_at = ? WHERE task_id = ?",
                (state.value, now, task_id),
            )


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
        task_id=TASK_ID,
        action_id="monitor.check",
        interval_seconds=300,
        start_at=start,
        payload={"query": "rain"},
        deadline_at=clock.value + timedelta(hours=1),
    )

    assert created.status is RecurrenceStatus.ACTIVE
    assert created.task_id == TASK_ID
    assert created.missed_run_policy is MissedRunPolicy.COALESCE_ONE
    assert created.next_due_at == start
    assert created.next_occurrence_id is not None
    assert scheduler.upserts[-1].trigger == {"run_date": start.isoformat()}
    assert scheduler.upserts[-1].payload["task_id"] == TASK_ID
    assert scheduler.upserts[-1].misfire_grace_seconds is None

    restarted, _ = _service(store, clock, calls)
    reloaded = restarted.get("weather Київ")
    assert reloaded == created


def test_real_apscheduler_adapter_reinstalls_next_date_intent_without_sleep(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _set_task_state(store, TASK_ID, TaskState.RUNNING)
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
        task_id=TASK_ID,
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


@pytest.mark.parametrize(
    "terminal_state",
    (TaskState.CANCELLED, TaskState.COMPLETED, TaskState.ARCHIVED),
)
def test_terminal_task_suppresses_recurrence_on_restart_before_handler(
    tmp_path: Path,
    terminal_state: TaskState,
) -> None:
    store = _store(tmp_path)
    _set_task_state(store, TASK_ID, TaskState.RUNNING)
    clock = FakeClock(datetime(2030, 1, 1, 12, 0, tzinfo=UTC))
    calls: list[RecurrenceInvocation] = []
    service, scheduler = _service(store, clock, calls)
    service.create(
        recurrence_id=f"terminal-{terminal_state.value.lower()}",
        task_id=TASK_ID,
        action_id="monitor.check",
        interval_seconds=60,
        start_at=clock.value,
    )
    persisted = scheduler.upserts[-1]
    _set_task_state(store, TASK_ID, terminal_state)

    jobs = ScheduledJobStore(store)
    restarted_ref: dict[str, DurableRecurrenceService] = {}

    def scheduler_resolver(action_id: str):
        assert action_id == DurableRecurrenceService.ACTION_ID
        return restarted_ref["service"].action_handler

    def target_resolver(action_id: str):
        assert action_id == "monitor.check"

        def handler(invocation: RecurrenceInvocation) -> None:
            calls.append(invocation)

        return handler

    adapter = APSchedulerAdapter(jobs, scheduler_resolver)
    restarted = DurableRecurrenceService(
        jobs=jobs,
        scheduler=adapter,
        handler_resolver=target_resolver,
        clock=clock,
    )
    restarted_ref["service"] = restarted

    adapter.start()
    try:
        assert not adapter.has_runtime_job(persisted.job_id)
        suppressed = jobs.get(persisted.job_id)
        assert suppressed is not None
        assert suppressed.enabled is False

        reconciled = restarted.get(f"terminal-{terminal_state.value.lower()}")
        assert reconciled is not None
        assert reconciled.status is RecurrenceStatus.CANCELLED
        assert reconciled.next_due_at is None
        assert reconciled.next_occurrence_id is None
        assert restarted.get(reconciled.recurrence_id) == reconciled
        assert calls == []
    finally:
        adapter.shutdown()


def test_missing_task_suppresses_recurrence_on_restart_before_handler(tmp_path: Path) -> None:
    store = _store(tmp_path)
    clock = FakeClock(datetime(2030, 1, 1, 12, 0, tzinfo=UTC))
    calls: list[RecurrenceInvocation] = []
    service, scheduler = _service(store, clock, calls)
    service.create(
        recurrence_id="missing-task",
        task_id=TASK_ID,
        action_id="monitor.check",
        interval_seconds=60,
        start_at=clock.value,
    )
    persisted = scheduler.upserts[-1]

    jobs = ScheduledJobStore(store)
    restarted_ref: dict[str, DurableRecurrenceService] = {}

    def scheduler_resolver(action_id: str):
        assert action_id == DurableRecurrenceService.ACTION_ID
        return restarted_ref["service"].action_handler

    def target_resolver(action_id: str):
        assert action_id == "monitor.check"

        def handler(invocation: RecurrenceInvocation) -> None:
            calls.append(invocation)

        return handler

    adapter = APSchedulerAdapter(jobs, scheduler_resolver)
    restarted = DurableRecurrenceService(
        jobs=jobs,
        scheduler=adapter,
        handler_resolver=target_resolver,
        clock=clock,
    )
    restarted_ref["service"] = restarted

    adapter.start()
    try:
        assert not adapter.has_runtime_job(persisted.job_id)
        suppressed = jobs.get(persisted.job_id)
        assert suppressed is not None
        assert suppressed.enabled is False

        reconciled = restarted.get("missing-task")
        assert reconciled is not None
        assert reconciled.status is RecurrenceStatus.CANCELLED
        assert reconciled.next_due_at is None
        assert reconciled.next_occurrence_id is None
        assert restarted.get("missing-task") == reconciled
        assert calls == []
    finally:
        adapter.shutdown()


def test_disabled_active_recurrence_with_nonterminal_task_remains_corrupt(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _set_task_state(store, TASK_ID, TaskState.RUNNING)
    clock = FakeClock(datetime(2030, 1, 1, 12, 0, tzinfo=UTC))
    calls: list[RecurrenceInvocation] = []
    service, scheduler = _service(store, clock, calls)
    service.create(
        recurrence_id="nonterminal-disabled",
        task_id=TASK_ID,
        action_id="monitor.check",
        interval_seconds=60,
        start_at=clock.value,
    )
    job_id = scheduler.upserts[-1].job_id
    assert service._jobs.set_enabled(job_id, False)

    with pytest.raises(ValueError, match="enabled state does not match lifecycle state"):
        service.get("nonterminal-disabled")

    assert calls == []


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
        task_id=TASK_ID,
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


@pytest.mark.parametrize(
    ("transition", "expected_status"),
    (
        ("pause", RecurrenceStatus.PAUSED),
        ("cancel", RecurrenceStatus.CANCELLED),
    ),
)
def test_resolver_lifecycle_change_blocks_stale_handler_effect(
    tmp_path: Path,
    transition: str,
    expected_status: RecurrenceStatus,
) -> None:
    store = _store(tmp_path)
    start = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    clock = FakeClock(start)
    calls: list[RecurrenceInvocation] = []
    jobs = ScheduledJobStore(store)
    scheduler = PersistingScheduler(jobs)
    service_ref: dict[str, DurableRecurrenceService] = {}

    def resolve(action_id: str):
        assert action_id == "monitor.check"
        service = service_ref["service"]
        getattr(service, transition)("resolver-lifecycle-fence")

        def handler(invocation: RecurrenceInvocation) -> None:
            calls.append(invocation)

        return handler

    service = DurableRecurrenceService(
        jobs=jobs,
        scheduler=scheduler,
        handler_resolver=resolve,
        clock=clock,
    )
    service_ref["service"] = service
    service.create(
        recurrence_id="resolver-lifecycle-fence",
        task_id=TASK_ID,
        action_id="monitor.check",
        interval_seconds=60,
        start_at=start,
    )

    service.action_handler({"recurrence_id": "resolver-lifecycle-fence"})

    state = service.get("resolver-lifecycle-fence")
    assert state is not None
    assert state.status is expected_status
    assert calls == []


def test_resolver_deadline_crossing_blocks_late_handler_effect(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    deadline = start + timedelta(minutes=1)
    clock = FakeClock(start)
    calls: list[RecurrenceInvocation] = []
    jobs = ScheduledJobStore(store)
    scheduler = PersistingScheduler(jobs)

    def resolve(action_id: str):
        assert action_id == "monitor.check"
        clock.advance(minutes=2)

        def handler(invocation: RecurrenceInvocation) -> None:
            calls.append(invocation)

        return handler

    service = DurableRecurrenceService(
        jobs=jobs,
        scheduler=scheduler,
        handler_resolver=resolve,
        clock=clock,
    )
    service.create(
        recurrence_id="resolver-deadline-fence",
        task_id=TASK_ID,
        action_id="monitor.check",
        interval_seconds=60,
        start_at=start,
        deadline_at=deadline,
    )

    service.action_handler({"recurrence_id": "resolver-deadline-fence"})

    state = service.get("resolver-deadline-fence")
    assert state is not None
    assert state.status is RecurrenceStatus.COMPLETED
    assert state.terminal_reason is RecurrenceTerminalReason.DEADLINE
    assert calls == []



def test_post_effect_deadline_terminalization_converges_without_false_failure(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    start = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    deadline = start + timedelta(minutes=1)
    clock = FakeClock(start)
    effects: list[str] = []
    jobs = ScheduledJobStore(store)
    scheduler = PersistingScheduler(jobs)
    service_ref: dict[str, DurableRecurrenceService] = {}

    def resolve(action_id: str):
        assert action_id == "monitor.check"

        def handler(invocation: RecurrenceInvocation) -> RecurrenceDecision:
            effects.append(invocation.occurrence_id)
            clock.advance(minutes=2)
            service_ref["service"].action_handler(
                {"recurrence_id": "post-effect-deadline"}
            )
            return RecurrenceDecision.CONTINUE

        return handler

    service = DurableRecurrenceService(
        jobs=jobs,
        scheduler=scheduler,
        handler_resolver=resolve,
        clock=clock,
    )
    service_ref["service"] = service
    service.create(
        recurrence_id="post-effect-deadline",
        task_id=TASK_ID,
        action_id="monitor.check",
        interval_seconds=60,
        start_at=start,
        deadline_at=deadline,
    )

    service.action_handler({"recurrence_id": "post-effect-deadline"})

    state = service.get("post-effect-deadline")
    assert state is not None
    assert state.status is RecurrenceStatus.COMPLETED
    assert state.terminal_reason is RecurrenceTerminalReason.DEADLINE
    assert state.next_due_at is None
    assert state.next_occurrence_id is None
    assert len(effects) == 1


def test_pause_survives_restart_and_resume_keeps_one_coalesced_intent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = datetime(2030, 1, 1, 12, 5, tzinfo=UTC)
    clock = FakeClock(start - timedelta(minutes=5))
    calls: list[RecurrenceInvocation] = []
    service, _ = _service(store, clock, calls)
    service.create(
        recurrence_id="paused-monitor",
        task_id=TASK_ID,
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
        task_id=TASK_ID,
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
        task_id=TASK_ID,
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
        task_id=TASK_ID,
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
        task_id=TASK_ID,
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
        task_id=TASK_ID,
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
        task_id=TASK_ID,
        action_id="monitor.check",
        interval_seconds=60,
        start_at=clock.value,
        payload={"scope": "one"},
    )

    with pytest.raises(ValueError, match="different recurrence"):
        service.create(
            recurrence_id="same-id",
            task_id=TASK_ID,
            action_id="monitor.check",
            interval_seconds=120,
            start_at=clock.value,
            payload={"scope": "one"},
        )
    with pytest.raises(ValueError, match="different recurrence"):
        service.create(
            recurrence_id="same-id",
            task_id="different-task",
            action_id="monitor.check",
            interval_seconds=60,
            start_at=clock.value,
            payload={"scope": "one"},
        )


def test_same_recurrence_id_payload_replay_uses_canonical_json_identity(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    clock = FakeClock(datetime(2030, 1, 1, 12, 0, tzinfo=UTC))
    calls: list[RecurrenceInvocation] = []
    service, _ = _service(store, clock, calls)
    created = service.create(
        recurrence_id="typed-payload",
        task_id=TASK_ID,
        action_id="monitor.check",
        interval_seconds=60,
        start_at=clock.value,
        payload={"enabled": True, "count": 1},
    )

    replayed = service.create(
        recurrence_id="typed-payload",
        task_id=TASK_ID,
        action_id="monitor.check",
        interval_seconds=60,
        start_at=clock.value,
        payload={"enabled": True, "count": 1},
    )
    assert replayed == created

    with pytest.raises(ValueError, match="different recurrence"):
        service.create(
            recurrence_id="typed-payload",
            task_id=TASK_ID,
            action_id="monitor.check",
            interval_seconds=60,
            start_at=clock.value,
            payload={"enabled": 1, "count": 1},
        )
    with pytest.raises(ValueError, match="different recurrence"):
        service.create(
            recurrence_id="typed-payload",
            task_id=TASK_ID,
            action_id="monitor.check",
            interval_seconds=60,
            start_at=clock.value,
            payload={"enabled": True, "count": 1.0},
        )

    assert service.get("typed-payload") == created
    assert calls == []


@pytest.mark.parametrize("bad_task_id", ("", " task", "task "))
def test_invalid_task_id_fails_before_persistence(
    tmp_path: Path,
    bad_task_id: str,
) -> None:
    store = _store(tmp_path)
    clock = FakeClock(datetime(2030, 1, 1, 12, 0, tzinfo=UTC))
    calls: list[RecurrenceInvocation] = []
    service, scheduler = _service(store, clock, calls)

    with pytest.raises(ValueError, match="task_id"):
        service.create(
            recurrence_id="bad-task-binding",
            task_id=bad_task_id,
            action_id="monitor.check",
            interval_seconds=60,
            start_at=clock.value,
        )
    assert scheduler.upserts == []
    assert service.get("bad-task-binding") is None


def test_naive_time_and_invalid_interval_fail_before_persistence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    clock = FakeClock(datetime(2030, 1, 1, 12, 0, tzinfo=UTC))
    calls: list[RecurrenceInvocation] = []
    service, _ = _service(store, clock, calls)

    with pytest.raises(ValueError, match="timezone-aware"):
        service.create(
            recurrence_id="naive",
            task_id=TASK_ID,
            action_id="monitor.check",
            interval_seconds=60,
            start_at=datetime.fromisoformat("2030-01-01T12:00:00"),
        )
    with pytest.raises(ValueError, match="positive integer"):
        service.create(
            recurrence_id="bad-interval",
            task_id=TASK_ID,
            action_id="monitor.check",
            interval_seconds=0,
            start_at=clock.value,
        )
    assert service.get("naive") is None
    assert service.get("bad-interval") is None


@pytest.mark.parametrize(
    ("start_at", "interval_seconds"),
    (
        (datetime(2030, 1, 1, 12, 0, tzinfo=UTC), 10**20),
        (datetime.max.replace(tzinfo=UTC) - timedelta(seconds=30), 60),
    ),
)
def test_unrepresentable_interval_fails_before_persistence_or_handler(
    tmp_path: Path,
    start_at: datetime,
    interval_seconds: int,
) -> None:
    store = _store(tmp_path)
    clock = FakeClock(datetime(2030, 1, 1, 12, 0, tzinfo=UTC))
    calls: list[RecurrenceInvocation] = []
    service, scheduler = _service(store, clock, calls)

    with pytest.raises(ValueError, match="cannot advance start_at"):
        service.create(
            recurrence_id="unrepresentable-interval",
            task_id=TASK_ID,
            action_id="monitor.check",
            interval_seconds=interval_seconds,
            start_at=start_at,
        )

    assert scheduler.upserts == []
    assert service.get("unrepresentable-interval") is None
    assert calls == []


def test_interval_int_subclass_fails_before_persistence(tmp_path: Path) -> None:
    class IntSubclass(int):
        pass

    store = _store(tmp_path)
    clock = FakeClock(datetime(2030, 1, 1, 12, 0, tzinfo=UTC))
    calls: list[RecurrenceInvocation] = []
    service, scheduler = _service(store, clock, calls)

    with pytest.raises(ValueError, match="positive integer"):
        service.create(
            recurrence_id="subclass-interval",
            task_id=TASK_ID,
            action_id="monitor.check",
            interval_seconds=IntSubclass(60),
            start_at=clock.value,
        )

    assert scheduler.upserts == []
    assert service.get("subclass-interval") is None
    assert calls == []


def test_clock_jump_persists_range_exhaustion_after_one_effect(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    clock = FakeClock(start)
    calls: list[RecurrenceInvocation] = []
    service, _ = _service(store, clock, calls)
    created = service.create(
        recurrence_id="range-exhaustion",
        task_id=TASK_ID,
        action_id="monitor.check",
        interval_seconds=60,
        start_at=start,
    )

    clock.value = datetime.max.replace(tzinfo=UTC) - timedelta(seconds=30)
    service.action_handler({"recurrence_id": "range-exhaustion"})

    assert len(calls) == 1
    state = service.get("range-exhaustion")
    assert state is not None
    assert state.status is RecurrenceStatus.COMPLETED
    assert state.terminal_reason is RecurrenceTerminalReason.RANGE_EXHAUSTED
    assert state.last_completed_occurrence_id == created.next_occurrence_id
    assert state.next_occurrence_id is None

    restarted, _ = _service(store, clock, calls)
    restarted.action_handler({"recurrence_id": "range-exhaustion"})
    assert len(calls) == 1


def test_behavioral_text_and_datetime_carriers_fail_before_behavior(tmp_path: Path) -> None:
    class BehavioralText(str):
        def strip(self, *args: object, **kwargs: object) -> str:
            del args, kwargs
            raise AssertionError("behavioral text method must not run")

    class BehavioralDatetime(datetime):
        def astimezone(self, *args: object, **kwargs: object) -> datetime:
            del args, kwargs
            raise AssertionError("behavioral datetime method must not run")

    store = _store(tmp_path)
    clock = FakeClock(datetime(2030, 1, 1, 12, 0, tzinfo=UTC))
    calls: list[RecurrenceInvocation] = []
    service, scheduler = _service(store, clock, calls)

    with pytest.raises(ValueError, match="recurrence_id"):
        service.create(
            recurrence_id=BehavioralText("hostile"),
            task_id=TASK_ID,
            action_id="monitor.check",
            interval_seconds=60,
            start_at=clock.value,
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        service.create(
            recurrence_id="hostile-datetime",
            task_id=TASK_ID,
            action_id="monitor.check",
            interval_seconds=60,
            start_at=BehavioralDatetime(
                2030,
                1,
                1,
                12,
                0,
                tzinfo=UTC,
            ),
        )

    assert scheduler.upserts == []
    assert calls == []


def test_optional_clock_is_selected_without_truthiness(tmp_path: Path) -> None:
    class BehavioralClock:
        def __init__(self, value: datetime) -> None:
            self.value = value
            self.calls = 0

        def __bool__(self) -> bool:
            raise AssertionError("clock truthiness must not run")

        def __call__(self) -> datetime:
            self.calls += 1
            return self.value

    store = _store(tmp_path)
    start = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    clock = BehavioralClock(start)
    calls: list[RecurrenceInvocation] = []
    jobs = ScheduledJobStore(store)
    scheduler = PersistingScheduler(jobs)

    def resolve(action_id: str):
        assert action_id == "monitor.check"

        def handler(invocation: RecurrenceInvocation) -> None:
            calls.append(invocation)

        return handler

    service = DurableRecurrenceService(
        jobs=jobs,
        scheduler=scheduler,
        handler_resolver=resolve,
        clock=clock,
    )
    created = service.create(
        recurrence_id="behavioral-clock",
        task_id=TASK_ID,
        action_id="monitor.check",
        interval_seconds=60,
        start_at=start,
        deadline_at=start + timedelta(hours=1),
    )

    assert created.status is RecurrenceStatus.ACTIVE
    assert clock.calls == 1
    assert len(scheduler.upserts) == 1
    assert calls == []


def test_datetime_timezone_carrier_fails_before_behavior(tmp_path: Path) -> None:
    class BehavioralTimezone(tzinfo):
        def utcoffset(self, dt: datetime | None) -> timedelta:
            del dt
            raise AssertionError("behavioral timezone offset must not run")

        def dst(self, dt: datetime | None) -> timedelta:
            del dt
            raise AssertionError("behavioral timezone dst must not run")

        def tzname(self, dt: datetime | None) -> str:
            del dt
            raise AssertionError("behavioral timezone name must not run")

    store = _store(tmp_path)
    clock = FakeClock(datetime(2030, 1, 1, 12, 0, tzinfo=UTC))
    calls: list[RecurrenceInvocation] = []
    service, scheduler = _service(store, clock, calls)
    hostile_time = datetime(
        2030,
        1,
        1,
        12,
        0,
        tzinfo=BehavioralTimezone(),
    )

    with pytest.raises(ValueError, match="canonical fixed offset"):
        service.create(
            recurrence_id="hostile-start-timezone",
            task_id=TASK_ID,
            action_id="monitor.check",
            interval_seconds=60,
            start_at=hostile_time,
        )

    clock.value = hostile_time
    valid_start = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="canonical fixed offset"):
        service.create(
            recurrence_id="hostile-clock-timezone",
            task_id=TASK_ID,
            action_id="monitor.check",
            interval_seconds=60,
            start_at=valid_start,
            deadline_at=valid_start + timedelta(hours=1),
        )

    clock.value = valid_start
    fixed_offset_start = datetime(
        2030,
        1,
        1,
        14,
        0,
        tzinfo=timezone(timedelta(hours=2)),
    )
    created = service.create(
        recurrence_id="fixed-offset-timezone",
        task_id=TASK_ID,
        action_id="monitor.check",
        interval_seconds=60,
        start_at=fixed_offset_start,
    )
    assert created.anchor_at == valid_start
    assert len(scheduler.upserts) == 1
    assert calls == []


def test_non_utf8_text_fails_before_hash_or_persistence(tmp_path: Path) -> None:
    bad_text = "\ud800"
    store = _store(tmp_path)
    clock = FakeClock(datetime(2030, 1, 1, 12, 0, tzinfo=UTC))
    calls: list[RecurrenceInvocation] = []
    service, scheduler = _service(store, clock, calls)

    with pytest.raises(ValueError, match="valid UTF-8"):
        service.create(
            recurrence_id=bad_text,
            task_id=TASK_ID,
            action_id="monitor.check",
            interval_seconds=60,
            start_at=clock.value,
        )
    with pytest.raises(ValueError, match="valid UTF-8"):
        service.create(
            recurrence_id="bad-task-utf8",
            task_id=bad_text,
            action_id="monitor.check",
            interval_seconds=60,
            start_at=clock.value,
        )
    with pytest.raises(ValueError, match="valid UTF-8"):
        service.create(
            recurrence_id="bad-action-utf8",
            task_id=TASK_ID,
            action_id=bad_text,
            interval_seconds=60,
            start_at=clock.value,
        )
    with pytest.raises(ValueError, match="valid UTF-8"):
        service.create(
            recurrence_id="bad-payload-value-utf8",
            task_id=TASK_ID,
            action_id="monitor.check",
            interval_seconds=60,
            start_at=clock.value,
            payload={"text": bad_text},
        )
    with pytest.raises(ValueError, match="valid UTF-8"):
        service.create(
            recurrence_id="bad-payload-key-utf8",
            task_id=TASK_ID,
            action_id="monitor.check",
            interval_seconds=60,
            start_at=clock.value,
            payload={bad_text: "value"},
        )
    with pytest.raises(ValueError, match="valid UTF-8"):
        service.action_handler(
            {
                "recurrence_id": "missing",
                bad_text: "ignored",
            }
        )

    assert scheduler.upserts == []
    assert calls == []


def test_payload_carriers_are_exact_json_and_detached_before_persistence(tmp_path: Path) -> None:
    class BehavioralDict(dict[str, object]):
        def items(self):
            raise AssertionError("behavioral dict method must not run")

    class BehavioralText(str):
        def encode(self, *args: object, **kwargs: object) -> bytes:
            del args, kwargs
            raise AssertionError("behavioral text encode must not run")

    store = _store(tmp_path)
    clock = FakeClock(datetime(2030, 1, 1, 12, 0, tzinfo=UTC))
    calls: list[RecurrenceInvocation] = []
    service, scheduler = _service(store, clock, calls)

    with pytest.raises(TypeError, match="exact dict"):
        service.create(
            recurrence_id="behavioral-dict",
            task_id=TASK_ID,
            action_id="monitor.check",
            interval_seconds=60,
            start_at=clock.value,
            payload=BehavioralDict({"query": "rain"}),
        )
    with pytest.raises(TypeError, match="exact JSON-compatible"):
        service.create(
            recurrence_id="behavioral-nested-text",
            task_id=TASK_ID,
            action_id="monitor.check",
            interval_seconds=60,
            start_at=clock.value,
            payload={"query": BehavioralText("rain")},
        )

    nested = ["original"]
    service.create(
        recurrence_id="detached-payload",
        task_id=TASK_ID,
        action_id="monitor.check",
        interval_seconds=60,
        start_at=clock.value,
        payload={"nested": nested},
    )
    nested.append("mutated-after-create")
    service.action_handler({"recurrence_id": "detached-payload"})

    assert calls[-1].payload == {"nested": ["original"]}
    assert len(scheduler.upserts) >= 2


def test_nonfinite_and_oversized_payloads_fail_before_persistence(tmp_path: Path) -> None:
    store = _store(tmp_path)
    clock = FakeClock(datetime(2030, 1, 1, 12, 0, tzinfo=UTC))
    calls: list[RecurrenceInvocation] = []
    service, scheduler = _service(store, clock, calls)

    with pytest.raises(ValueError, match="non-finite"):
        service.create(
            recurrence_id="nonfinite-payload",
            task_id=TASK_ID,
            action_id="monitor.check",
            interval_seconds=60,
            start_at=clock.value,
            payload={"score": float("nan")},
        )
    with pytest.raises(ValueError, match="size limit"):
        service.create(
            recurrence_id="oversized-payload",
            task_id=TASK_ID,
            action_id="monitor.check",
            interval_seconds=60,
            start_at=clock.value,
            payload={"text": "x" * 262_145},
        )

    assert scheduler.upserts == []
    assert calls == []


def test_action_payload_requires_exact_dict_before_lookup(tmp_path: Path) -> None:
    class BehavioralActionPayload(dict[str, object]):
        def get(self, *args: object, **kwargs: object) -> object:
            del args, kwargs
            raise AssertionError("behavioral payload get must not run")

    store = _store(tmp_path)
    clock = FakeClock(datetime(2030, 1, 1, 12, 0, tzinfo=UTC))
    calls: list[RecurrenceInvocation] = []
    service, _ = _service(store, clock, calls)

    with pytest.raises(TypeError, match="exact dict"):
        service.action_handler(
            BehavioralActionPayload({"recurrence_id": "does-not-matter"})
        )
    assert calls == []


def test_restart_rejects_nonfinite_persisted_target_payload(tmp_path: Path) -> None:
    store = _store(tmp_path)
    clock = FakeClock(datetime(2030, 1, 1, 12, 0, tzinfo=UTC))
    calls: list[RecurrenceInvocation] = []
    service, scheduler = _service(store, clock, calls)
    service.create(
        recurrence_id="corrupt-payload",
        task_id=TASK_ID,
        action_id="monitor.check",
        interval_seconds=60,
        start_at=clock.value,
        payload={"score": 1.0},
    )
    job_id = scheduler.upserts[-1].job_id

    with store.connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM scheduled_jobs WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        assert row is not None
        persisted = json.loads(row["payload_json"])
        persisted["target_payload"]["score"] = float("nan")
        conn.execute(
            "UPDATE scheduled_jobs SET payload_json = ? WHERE job_id = ?",
            (json.dumps(persisted, sort_keys=True), job_id),
        )

    restarted, _ = _service(store, clock, calls)
    with pytest.raises(ValueError, match="non-finite"):
        restarted.get("corrupt-payload")
    assert calls == []


def test_persisted_enum_carriers_fail_before_enum_behavior(tmp_path: Path) -> None:
    class BehavioralText(str):
        def __hash__(self) -> int:
            raise AssertionError("behavioral enum carrier must not be hashed")

    store = _store(tmp_path)
    clock = FakeClock(datetime(2030, 1, 1, 12, 0, tzinfo=UTC))
    calls: list[RecurrenceInvocation] = []
    service, scheduler = _service(store, clock, calls)
    service.create(
        recurrence_id="hostile-enum-state",
        task_id=TASK_ID,
        action_id="monitor.check",
        interval_seconds=60,
        start_at=clock.value,
    )
    job = scheduler.upserts[-1]
    metadata = job.payload["_nika_recurrence_v1"]
    assert type(metadata) is dict
    metadata["status"] = BehavioralText("active")

    with pytest.raises(ValueError, match="enum state is corrupt"):
        recurrence_module._decode_job(
            job,
            expected_recurrence_id="hostile-enum-state",
        )
    assert calls == []


def test_durable_transport_authority_rejects_scheduler_drift(tmp_path: Path) -> None:
    store = _store(tmp_path)
    clock = FakeClock(datetime(2030, 1, 1, 12, 0, tzinfo=UTC))
    calls: list[RecurrenceInvocation] = []
    service, scheduler = _service(store, clock, calls)
    service.create(
        recurrence_id="transport-authority",
        task_id=TASK_ID,
        action_id="monitor.check",
        interval_seconds=60,
        start_at=clock.value,
    )
    job = scheduler.upserts[-1]
    wrong_payload = dict(job.payload)
    wrong_payload["recurrence_id"] = "different-recurrence"

    bad_jobs = (
        (replace(job, trigger_kind=TriggerKind.INTERVAL), "DATE trigger"),
        (
            replace(
                job,
                trigger={
                    "run_date": job.trigger["run_date"],
                    "seconds": 60,
                },
            ),
            "trigger shape",
        ),
        (
            replace(
                job,
                trigger={
                    "run_date": (
                        clock.value + timedelta(minutes=5)
                    ).isoformat(),
                },
            ),
            "trigger run_date",
        ),
        (replace(job, coalesce=False), "coalesce policy"),
        (replace(job, max_instances=2), "max_instances policy"),
        (replace(job, misfire_grace_seconds=60), "misfire policy"),
        (replace(job, enabled=1), "enabled state"),
        (
            replace(job, payload=wrong_payload),
            "scheduled identity mismatch",
        ),
    )

    for bad_job, message in bad_jobs:
        with pytest.raises(ValueError, match=message):
            recurrence_module._decode_job(
                bad_job,
                expected_recurrence_id="transport-authority",
            )
    assert calls == []


def test_persisted_scalar_carriers_fail_before_behavior(tmp_path: Path) -> None:
    class BehavioralText(str):
        def __eq__(self, other: object) -> bool:
            del other
            raise AssertionError("behavioral text comparison must not run")

        def __ne__(self, other: object) -> bool:
            del other
            raise AssertionError("behavioral text comparison must not run")

    class BehavioralInt(int):
        def __eq__(self, other: object) -> bool:
            del other
            raise AssertionError("behavioral integer comparison must not run")

        def __ne__(self, other: object) -> bool:
            del other
            raise AssertionError("behavioral integer comparison must not run")

    store = _store(tmp_path)
    clock = FakeClock(datetime(2030, 1, 1, 12, 0, tzinfo=UTC))
    calls: list[RecurrenceInvocation] = []
    service, scheduler = _service(store, clock, calls)
    service.create(
        recurrence_id="scalar-authority",
        task_id=TASK_ID,
        action_id="monitor.check",
        interval_seconds=60,
        start_at=clock.value,
    )
    job = scheduler.upserts[-1]

    version_payload = dict(job.payload)
    version_metadata = dict(version_payload["_nika_recurrence_v1"])
    version_metadata["version"] = BehavioralInt(2)
    version_payload["_nika_recurrence_v1"] = version_metadata

    binding_payload = dict(job.payload)
    binding_payload["_nika_immutable_job_binding_v1"] = BehavioralText(
        binding_payload["_nika_immutable_job_binding_v1"]
    )

    bad_jobs = (
        (
            replace(job, job_id=BehavioralText(job.job_id)),
            "job identity",
        ),
        (
            replace(job, action_id=BehavioralText(job.action_id)),
            "unexpected action_id",
        ),
        (
            replace(job, payload=version_payload),
            "payload version",
        ),
        (
            replace(job, payload=binding_payload),
            "immutable binding",
        ),
    )
    for bad_job, message in bad_jobs:
        with pytest.raises(ValueError, match=message):
            recurrence_module._decode_job(
                bad_job,
                expected_recurrence_id="scalar-authority",
            )
    assert calls == []


def test_persisted_timeline_and_terminal_semantics_fail_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    clock = FakeClock(start)
    calls: list[RecurrenceInvocation] = []
    service, scheduler = _service(store, clock, calls)
    service.create(
        recurrence_id="timeline-authority",
        task_id=TASK_ID,
        action_id="monitor.check",
        interval_seconds=60,
        start_at=start,
    )
    job = scheduler.upserts[-1]

    off_grid_payload = dict(job.payload)
    off_grid_metadata = dict(off_grid_payload["_nika_recurrence_v1"])
    off_grid_due = start + timedelta(seconds=30)
    off_grid_metadata["next_due_at"] = off_grid_due.isoformat()
    off_grid_metadata["next_occurrence_id"] = recurrence_module._occurrence_id(
        "timeline-authority",
        off_grid_due,
    )
    off_grid_payload["_nika_recurrence_v1"] = off_grid_metadata
    off_grid_job = replace(
        job,
        payload=off_grid_payload,
        trigger={"run_date": off_grid_due.isoformat()},
    )
    with pytest.raises(ValueError, match="outside the recurrence grid"):
        recurrence_module._decode_job(
            off_grid_job,
            expected_recurrence_id="timeline-authority",
        )

    active_terminal_payload = dict(job.payload)
    active_terminal_metadata = dict(active_terminal_payload["_nika_recurrence_v1"])
    active_terminal_metadata["terminal_reason"] = "deadline"
    active_terminal_metadata["deadline_at"] = (start + timedelta(minutes=5)).isoformat()
    active_terminal_payload["_nika_recurrence_v1"] = active_terminal_metadata
    with pytest.raises(ValueError, match="non-completed recurrence"):
        recurrence_module._decode_job(
            replace(job, payload=active_terminal_payload),
            expected_recurrence_id="timeline-authority",
        )
    assert calls == []


def test_persisted_deadline_semantics_fail_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    start = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    deadline = start + timedelta(minutes=5)
    clock = FakeClock(start)
    calls: list[RecurrenceInvocation] = []
    service, scheduler = _service(store, clock, calls)
    service.create(
        recurrence_id="deadline-authority",
        task_id=TASK_ID,
        action_id="monitor.check",
        interval_seconds=60,
        start_at=start,
        deadline_at=deadline,
    )
    job = scheduler.upserts[-1]

    late_next_payload = dict(job.payload)
    late_next_metadata = dict(late_next_payload["_nika_recurrence_v1"])
    late_next_metadata["next_due_at"] = deadline.isoformat()
    late_next_metadata["next_occurrence_id"] = recurrence_module._occurrence_id(
        "deadline-authority",
        deadline,
    )
    late_next_payload["_nika_recurrence_v1"] = late_next_metadata
    with pytest.raises(ValueError, match="next intent must be before deadline"):
        recurrence_module._decode_job(
            replace(
                job,
                payload=late_next_payload,
                trigger={"run_date": deadline.isoformat()},
            ),
            expected_recurrence_id="deadline-authority",
        )

    late_last_payload = dict(job.payload)
    late_last_metadata = dict(late_last_payload["_nika_recurrence_v1"])
    late_last_metadata.update(
        {
            "status": "cancelled",
            "next_due_at": None,
            "next_occurrence_id": None,
            "last_completed_due_at": deadline.isoformat(),
            "last_completed_occurrence_id": recurrence_module._occurrence_id(
                "deadline-authority",
                deadline,
            ),
        }
    )
    late_last_payload["_nika_recurrence_v1"] = late_last_metadata
    with pytest.raises(ValueError, match="completion cursor must be before deadline"):
        recurrence_module._decode_job(
            replace(
                job,
                payload=late_last_payload,
                enabled=False,
                trigger={"run_date": deadline.isoformat()},
            ),
            expected_recurrence_id="deadline-authority",
        )

    service.create(
        recurrence_id="missing-deadline-authority",
        task_id=TASK_ID,
        action_id="monitor.check",
        interval_seconds=60,
        start_at=start,
    )
    no_deadline_job = scheduler.upserts[-1]
    missing_payload = dict(no_deadline_job.payload)
    missing_metadata = dict(missing_payload["_nika_recurrence_v1"])
    missing_metadata.update(
        {
            "status": "completed",
            "next_due_at": None,
            "next_occurrence_id": None,
            "terminal_reason": "deadline",
        }
    )
    missing_payload["_nika_recurrence_v1"] = missing_metadata
    with pytest.raises(ValueError, match="terminal reason is missing deadline"):
        recurrence_module._decode_job(
            replace(no_deadline_job, payload=missing_payload, enabled=False),
            expected_recurrence_id="missing-deadline-authority",
        )

    immediate = service.create(
        recurrence_id="immediate-deadline",
        task_id=TASK_ID,
        action_id="monitor.check",
        interval_seconds=60,
        start_at=start,
        deadline_at=start,
    )
    assert immediate.status is RecurrenceStatus.COMPLETED
    assert immediate.terminal_reason is RecurrenceTerminalReason.DEADLINE
    assert service.get("immediate-deadline") == immediate
    assert calls == []


def test_persisted_mapping_keys_fail_before_lookup_behavior(tmp_path: Path) -> None:
    class BehavioralKey(str):
        __hash__ = str.__hash__

        def __eq__(self, other: object) -> bool:
            del other
            raise AssertionError("behavioral persisted key comparison must not run")

    store = _store(tmp_path)
    clock = FakeClock(datetime(2030, 1, 1, 12, 0, tzinfo=UTC))
    calls: list[RecurrenceInvocation] = []
    service, scheduler = _service(store, clock, calls)
    service.create(
        recurrence_id="key-authority",
        task_id=TASK_ID,
        action_id="monitor.check",
        interval_seconds=60,
        start_at=clock.value,
    )
    job = scheduler.upserts[-1]

    trigger = dict(job.trigger)
    run_date = trigger.pop("run_date")
    trigger[BehavioralKey("run_date")] = run_date
    with pytest.raises(TypeError, match="durable recurrence trigger keys"):
        recurrence_module._decode_job(
            replace(job, trigger=trigger),
            expected_recurrence_id="key-authority",
        )

    payload = dict(job.payload)
    recurrence_value = payload.pop("recurrence_id")
    payload[BehavioralKey("recurrence_id")] = recurrence_value
    with pytest.raises(TypeError, match="durable recurrence payload keys"):
        recurrence_module._decode_job(
            replace(job, payload=payload),
            expected_recurrence_id="key-authority",
        )

    metadata_payload = dict(job.payload)
    metadata = dict(metadata_payload["_nika_recurrence_v1"])
    version = metadata.pop("version")
    metadata[BehavioralKey("version")] = version
    metadata_payload["_nika_recurrence_v1"] = metadata
    with pytest.raises(TypeError, match="durable recurrence metadata keys"):
        recurrence_module._decode_job(
            replace(job, payload=metadata_payload),
            expected_recurrence_id="key-authority",
        )

    assert calls == []


def test_action_payload_keys_fail_before_lookup_behavior(tmp_path: Path) -> None:
    class BehavioralKey(str):
        __hash__ = str.__hash__

        def __eq__(self, other: object) -> bool:
            del other
            raise AssertionError("behavioral action key comparison must not run")

    store = _store(tmp_path)
    clock = FakeClock(datetime(2030, 1, 1, 12, 0, tzinfo=UTC))
    calls: list[RecurrenceInvocation] = []
    service, _ = _service(store, clock, calls)
    action_payload: dict[str, object] = {}
    action_payload[BehavioralKey("recurrence_id")] = "unknown"

    with pytest.raises(TypeError, match="recurrence action payload keys"):
        service.action_handler(action_payload)  # type: ignore[arg-type]

    assert calls == []
