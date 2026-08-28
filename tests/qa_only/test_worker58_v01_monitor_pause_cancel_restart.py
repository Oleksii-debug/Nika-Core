from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.checkpoint import CheckpointService
from nika_core.kernel.task_queue import TaskQueue
from nika_core.research.models import ExtractedDocument, ResearchWorkspace, SourceKind, SourceSpec
from nika_core.research.network_repository import NetworkResearchRepository
from nika_core.research.profile_jobs import ResearchProfileRunService
from nika_core.research.profiles import (
    ResearchProfile,
    ResearchProfileRepository,
    ResearchSourceRef,
    ResearchSourceSet,
)
from nika_core.research.query import DeterministicResearchQueryService
from nika_core.research.repository import ResearchRepository
from nika_core.research.scheduled_profiles import ScheduledResearchProfileService
from nika_core.scheduler.contracts import ScheduledJob
from nika_core.scheduler.recurrence import (
    DurableRecurrenceService,
    RecurrenceInvocation,
    RecurrenceStatus,
)
from nika_core.scheduler.store import ScheduledJobStore

_RECURRENCE_ID = "worker58-grant-monitor"
_SERIES_ID = "worker58-grant-series"
_PROFILE_ID = "worker58-grant-profile"


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

    def start(self) -> None:
        return None

    def shutdown(self, *, wait: bool = True) -> None:
        del wait

    def upsert(self, job: ScheduledJob) -> None:
        self.jobs.upsert(job)

    def remove(self, job_id: str) -> bool:
        return self.jobs.delete(job_id)

    def pause(self, job_id: str) -> None:
        self.jobs.set_enabled(job_id, False)

    def resume(self, job_id: str) -> None:
        self.jobs.set_enabled(job_id, True)


class NoopScheduler:
    def start(self) -> None:
        return None

    def shutdown(self, *, wait: bool = True) -> None:
        del wait

    def upsert(self, job: ScheduledJob) -> None:
        del job

    def remove(self, job_id: str) -> bool:
        del job_id
        return False

    def pause(self, job_id: str) -> None:
        del job_id

    def resume(self, job_id: str) -> None:
        del job_id


class NoopWeb:
    def refresh_source(self, source_id: str, *, task_id: str | None = None) -> None:
        del source_id, task_id
        raise AssertionError("local deterministic monitor must not fetch HTTP")


def _seed_research(store: SQLiteStore) -> None:
    repository = ResearchRepository(store)
    repository.upsert_workspace(ResearchWorkspace("worker58-ws", "Worker 58 Research"))
    source = SourceSpec(
        "worker58-local",
        "worker58-ws",
        SourceKind.LOCAL_FILE,
        "C:/Nika Fixtures/worker58 grant.txt",
    )
    repository.upsert_source(source)
    repository.ingest_document(
        source,
        ExtractedDocument(
            "Grant notice",
            "Український освітній грант для студентів.",
            "text/plain",
        ),
    )

    profiles = ResearchProfileRepository(store)
    profiles.save_source_set(
        ResearchSourceSet(
            "worker58-source-set",
            "worker58-ws",
            1,
            "Worker 58 sources",
            (ResearchSourceRef("worker58-local", SourceKind.LOCAL_FILE),),
        )
    )
    profiles.save_profile(
        ResearchProfile(
            _PROFILE_ID,
            "worker58-ws",
            1,
            "Worker 58 monitor",
            "worker58-source-set",
            1,
            "освітній грант",
        )
    )


def _scheduled_research(store: SQLiteStore) -> ScheduledResearchProfileService:
    profiles = ResearchProfileRepository(store)
    network = NetworkResearchRepository(store)
    runs = ResearchProfileRunService(
        tasks=TaskQueue(store),
        checkpoints=CheckpointService(store),
        profiles=profiles,
        network_repository=network,
        query_service=DeterministicResearchQueryService(
            store=store,
            network_repository=network,
        ),
        web=NoopWeb(),  # type: ignore[arg-type]
    )
    return ScheduledResearchProfileService(
        store=store,
        scheduler=NoopScheduler(),  # type: ignore[arg-type]
        profiles=profiles,
        runs=runs,
        network_repository=network,
    )


def _recurrence(
    store: SQLiteStore,
    clock: FakeClock,
    scheduled: ScheduledResearchProfileService,
) -> DurableRecurrenceService:
    jobs = ScheduledJobStore(store)

    def resolve(action_id: str):
        assert action_id == ScheduledResearchProfileService.ACTION_ID

        def run(invocation: RecurrenceInvocation) -> None:
            scheduled.run_scheduled(invocation.payload)

        return run

    return DurableRecurrenceService(
        jobs=jobs,
        scheduler=PersistingScheduler(jobs),  # type: ignore[arg-type]
        handler_resolver=resolve,
        clock=clock,
    )


def _history_task_ids(store: SQLiteStore) -> tuple[str, ...]:
    with store.connection() as conn:
        rows = conn.execute(
            "SELECT task_id FROM research_profile_run_history "
            "WHERE series_id=? ORDER BY rowid",
            (_SERIES_ID,),
        ).fetchall()
    return tuple(row["task_id"] for row in rows)


def _payload() -> dict[str, Any]:
    return {
        "series_id": _SERIES_ID,
        "profile_id": _PROFILE_ID,
        "profile_version": 1,
    }


def _reopen(path: Path) -> SQLiteStore:
    store = SQLiteStore(path)
    store.initialize()
    return store


def test_monitor_pause_cancel_restart_preserves_exactly_once_checks_and_report(
    tmp_path: Path,
) -> None:
    path = tmp_path / "Nika Worker 58 монітор.db"
    store = _reopen(path)
    _seed_research(store)
    start = datetime(2030, 1, 1, 12, 0, tzinfo=UTC)
    clock = FakeClock(start)
    scheduled = _scheduled_research(store)
    recurrence = _recurrence(store, clock, scheduled)

    recurrence.create(
        recurrence_id=_RECURRENCE_ID,
        action_id=ScheduledResearchProfileService.ACTION_ID,
        interval_seconds=300,
        start_at=start,
        payload=_payload(),
    )
    recurrence.action_handler({"recurrence_id": _RECURRENCE_ID})

    first_history = _history_task_ids(store)
    assert len(first_history) == 1
    first_delta = scheduled.delta_for_task(first_history[0])
    first_report = scheduled.render_delta_text(first_delta)
    assert "New: 1" in first_report

    paused = recurrence.pause(_RECURRENCE_ID)
    assert paused.status is RecurrenceStatus.PAUSED
    assert paused.next_due_at == start + timedelta(minutes=5)

    clock.advance(minutes=15)
    restarted_store = _reopen(path)
    restarted_scheduled = _scheduled_research(restarted_store)
    restarted = _recurrence(restarted_store, clock, restarted_scheduled)

    assert restarted.get(_RECURRENCE_ID) == paused
    assert restarted_scheduled.delta_for_task(first_history[0]) == first_delta
    assert restarted_scheduled.render_delta_text(first_delta) == first_report

    restarted.action_handler({"recurrence_id": _RECURRENCE_ID})
    assert _history_task_ids(restarted_store) == first_history

    resumed = restarted.resume(_RECURRENCE_ID)
    assert resumed.status is RecurrenceStatus.ACTIVE
    assert resumed.next_due_at == start + timedelta(minutes=5)
    restarted.action_handler({"recurrence_id": _RECURRENCE_ID})

    resumed_history = _history_task_ids(restarted_store)
    assert len(resumed_history) == 2
    assert resumed_history[0] == first_history[0]
    assert resumed_history[1] != first_history[0]
    second_delta = restarted_scheduled.delta_for_task(resumed_history[1])
    assert second_delta.previous_result_set_id == first_delta.result_set_id
    assert second_delta.items == ()
    assert "No new or changed matching results." in restarted_scheduled.render_delta_text(
        second_delta
    )

    restarted.action_handler({"recurrence_id": _RECURRENCE_ID})
    assert _history_task_ids(restarted_store) == resumed_history

    cancelled = restarted.cancel(_RECURRENCE_ID)
    assert cancelled.status is RecurrenceStatus.CANCELLED
    assert cancelled.next_due_at is None
    assert cancelled.next_occurrence_id is None

    clock.advance(days=7)
    after_cancel_store = _reopen(path)
    after_cancel_scheduled = _scheduled_research(after_cancel_store)
    after_cancel = _recurrence(after_cancel_store, clock, after_cancel_scheduled)

    assert after_cancel.get(_RECURRENCE_ID) == cancelled
    after_cancel.action_handler({"recurrence_id": _RECURRENCE_ID})
    assert _history_task_ids(after_cancel_store) == resumed_history
    assert after_cancel_scheduled.delta_for_task(first_history[0]) == first_delta
    assert after_cancel_scheduled.render_delta_text(first_delta) == first_report
    assert after_cancel_scheduled.delta_for_task(resumed_history[1]) == second_delta
