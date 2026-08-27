from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.research.monitor_until import (
    MonitorConditionState,
    MonitorRunState,
    MonitorStopReason,
    MonitorUntilConditionService,
)
from nika_core.scheduler import ScheduledJob, ScheduledJobStore, TriggerKind


class _FakeClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def set(self, now: datetime) -> None:
        self.now = now


class _StoreBackedScheduler:
    def __init__(self, jobs: ScheduledJobStore) -> None:
        self.jobs = jobs
        self.runtime_jobs: dict[str, ScheduledJob] = {}

    def start(self) -> None:
        self.runtime_jobs = {job.job_id: job for job in self.jobs.list_enabled()}

    def shutdown(self, *, wait: bool = True) -> None:
        del wait
        self.runtime_jobs.clear()

    def upsert(self, job: ScheduledJob) -> None:
        self.jobs.upsert(job)
        if job.enabled:
            self.runtime_jobs[job.job_id] = job
        else:
            self.runtime_jobs.pop(job.job_id, None)

    def remove(self, job_id: str) -> bool:
        removed = self.jobs.delete(job_id)
        self.runtime_jobs.pop(job_id, None)
        return removed

    def pause(self, job_id: str) -> None:
        job = self._required(job_id)
        self.jobs.set_enabled(job_id, False)
        self.runtime_jobs.pop(job_id, None)
        assert replace(job, enabled=False) == self.jobs.get(job_id)

    def resume(self, job_id: str) -> None:
        job = self._required(job_id)
        self.jobs.set_enabled(job_id, True)
        enabled = replace(job, enabled=True)
        self.runtime_jobs[job_id] = enabled
        assert enabled == self.jobs.get(job_id)

    def _required(self, job_id: str) -> ScheduledJob:
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job


def _dt(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, 27, hour, minute, tzinfo=UTC)


def _job(*, trigger: dict[str, object] | None = None) -> ScheduledJob:
    return ScheduledJob(
        job_id="monitor-a",
        action_id="research.monitor.tick",
        trigger_kind=TriggerKind.INTERVAL,
        trigger=trigger or {"minutes": 5},
        payload={"series_id": "series-a"},
        enabled=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_seconds=60,
    )


def _stack(tmp_path: Path, now: datetime):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    jobs = ScheduledJobStore(store)
    scheduler = _StoreBackedScheduler(jobs)
    clock = _FakeClock(now)
    service = MonitorUntilConditionService(
        scheduler=scheduler,  # type: ignore[arg-type]
        jobs=jobs,
        clock=clock,
    )
    return store, jobs, scheduler, clock, service


def test_condition_before_deadline_stops_future_runs_and_survives_restart(
    tmp_path: Path,
) -> None:
    store, jobs, scheduler, clock, service = _stack(tmp_path, _dt(17))
    status = service.register(_job(), deadline_at=_dt(18))

    assert status.run_state is MonitorRunState.ACTIVE
    assert jobs.get("monitor-a::deadline") is not None
    assert service.before_check("monitor-a") is True

    clock.set(_dt(17, 30))
    stopped = service.record_condition(
        "monitor-a",
        matched=True,
        observed_at=clock(),
    )

    assert stopped.stop_reason is MonitorStopReason.CONDITION_MET
    assert stopped.condition_state is MonitorConditionState.MATCHED
    assert stopped.stopped_at == _dt(17, 30)
    assert jobs.get("monitor-a").enabled is False
    assert jobs.get("monitor-a::deadline") is None
    assert "monitor-a" not in scheduler.runtime_jobs

    restarted_jobs = ScheduledJobStore(SQLiteStore(store.path))
    restarted_scheduler = _StoreBackedScheduler(restarted_jobs)
    restarted_scheduler.start()
    restarted = MonitorUntilConditionService(
        scheduler=restarted_scheduler,  # type: ignore[arg-type]
        jobs=restarted_jobs,
        clock=_FakeClock(_dt(17, 45)),
    )

    replay = restarted.register(_job(), deadline_at=_dt(18))
    assert replay.stop_reason is MonitorStopReason.CONDITION_MET
    assert restarted.before_check("monitor-a") is False
    assert restarted.resume("monitor-a").stop_reason is MonitorStopReason.CONDITION_MET
    assert "monitor-a" not in restarted_scheduler.runtime_jobs


def test_deadline_guard_stops_monitor_and_removes_future_jobs(tmp_path: Path) -> None:
    _, jobs, scheduler, clock, service = _stack(tmp_path, _dt(17))
    service.register(_job(), deadline_at=_dt(18))
    clock.set(_dt(18))

    service.deadline_action_handler({"schedule_id": "monitor-a"})
    status = service.status("monitor-a")

    assert status.stop_reason is MonitorStopReason.DEADLINE_REACHED
    assert status.condition_state is MonitorConditionState.PENDING
    assert status.stopped_at == _dt(18)
    assert jobs.get("monitor-a").enabled is False
    assert jobs.get("monitor-a::deadline") is None
    assert scheduler.runtime_jobs == {}


def test_before_check_fails_closed_when_deadline_passed_during_downtime(
    tmp_path: Path,
) -> None:
    _, jobs, _, clock, service = _stack(tmp_path, _dt(17))
    service.register(_job(), deadline_at=_dt(18))

    clock.set(_dt(18, 5))
    assert service.before_check("monitor-a") is False

    status = service.status("monitor-a")
    assert status.stop_reason is MonitorStopReason.DEADLINE_REACHED
    assert status.stopped_at == _dt(18)
    assert jobs.get("monitor-a").enabled is False
    assert jobs.get("monitor-a::deadline") is None


def test_exact_deadline_tie_is_deterministically_deadline_first(tmp_path: Path) -> None:
    _, _, _, _, service = _stack(tmp_path, _dt(17))
    service.register(_job(), deadline_at=_dt(18))

    status = service.record_condition(
        "monitor-a",
        matched=True,
        observed_at=_dt(18),
    )

    assert status.condition_state is MonitorConditionState.MATCHED
    assert status.stop_reason is MonitorStopReason.DEADLINE_REACHED
    assert status.stopped_at == _dt(18)
    assert "deadline reached" in service.render_status_text(status).casefold()
    assert "exact deadline" in service.render_status_text(status).casefold()


def test_unmatched_condition_is_canonical_and_monitor_remains_active(
    tmp_path: Path,
) -> None:
    store, _, _, _, service = _stack(tmp_path, _dt(17))
    service.register(_job(), deadline_at=_dt(18))

    status = service.record_condition(
        "monitor-a",
        matched=False,
        observed_at=_dt(17, 20),
    )

    assert status.condition_state is MonitorConditionState.NOT_MET
    assert status.stop_reason is None
    assert status.run_state is MonitorRunState.ACTIVE

    restarted_jobs = ScheduledJobStore(SQLiteStore(store.path))
    restarted_scheduler = _StoreBackedScheduler(restarted_jobs)
    restarted_scheduler.start()
    restarted = MonitorUntilConditionService(
        scheduler=restarted_scheduler,  # type: ignore[arg-type]
        jobs=restarted_jobs,
        clock=_FakeClock(_dt(17, 25)),
    )
    persisted = restarted.status("monitor-a")
    assert persisted.condition_state is MonitorConditionState.NOT_MET
    assert persisted.last_observed_at == _dt(17, 20)
    assert persisted.run_state is MonitorRunState.ACTIVE
    assert {"monitor-a", "monitor-a::deadline"} <= set(restarted_scheduler.runtime_jobs)


def test_final_status_text_explains_condition_stop(tmp_path: Path) -> None:
    _, _, _, _, service = _stack(tmp_path, _dt(17))
    service.register(_job(), deadline_at=_dt(18))
    status = service.record_condition(
        "monitor-a",
        matched=True,
        observed_at=_dt(17, 10),
    )

    text = service.render_status_text(status)

    assert "State: stopped" in text
    assert "Condition: matched" in text
    assert "Stopped because: condition matched before the deadline." in text
    assert "Deadline: 2026-08-27T18:00:00+00:00" in text


def test_canonical_deadline_cannot_be_duplicated_or_silently_changed(
    tmp_path: Path,
) -> None:
    _, _, _, _, service = _stack(tmp_path, _dt(17))

    with pytest.raises(ValueError, match="not trigger end_date"):
        service.register(
            _job(trigger={"minutes": 5, "end_date": _dt(18).isoformat()}),
            deadline_at=_dt(18),
        )

    service.register(_job(), deadline_at=_dt(18))
    with pytest.raises(ValueError, match="different canonical deadline"):
        service.register(_job(), deadline_at=_dt(19))


def test_foreign_or_malformed_monitor_state_fails_clearly(tmp_path: Path) -> None:
    _, jobs, scheduler, _, service = _stack(tmp_path, _dt(17))
    scheduler.upsert(_job())

    with pytest.raises(ValueError, match="is not a monitor-until job"):
        service.status("monitor-a")

    malformed = replace(
        _job(),
        payload={
            "series_id": "series-a",
            MonitorUntilConditionService.META_KEY: {
                "version": MonitorUntilConditionService.META_VERSION,
                "deadline_at": "not-a-time",
                "condition_state": "pending",
                "stop_reason": None,
                "stopped_at": None,
                "last_observed_at": None,
            },
        },
    )
    scheduler.upsert(malformed)

    with pytest.raises(ValueError, match="deadline_at must be an ISO-8601 datetime"):
        service.status("monitor-a")
