from __future__ import annotations

import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from nika_core.data.sqlite import SQLiteStore
from nika_core.research.monitor_until import (
    MonitorConditionState,
    MonitorStopReason,
    MonitorUntilConditionService,
)
from nika_core.scheduler import ScheduledJob, ScheduledJobStore, TriggerKind

_SYNC_TIMEOUT_SECONDS = 10.0
_MONITOR_ID = "monitor-worker59"


class _FakeClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class _DirectScheduler:
    def __init__(self, jobs: ScheduledJobStore) -> None:
        self.jobs = jobs

    def start(self) -> None:
        pass

    def shutdown(self, *, wait: bool = True) -> None:
        del wait

    def upsert(self, job: ScheduledJob) -> None:
        self.jobs.upsert(job)

    def remove(self, job_id: str) -> bool:
        return self.jobs.delete(job_id)

    def pause(self, job_id: str) -> None:
        job = self._required(job_id)
        self.jobs.upsert(replace(job, enabled=False))

    def resume(self, job_id: str) -> None:
        job = self._required(job_id)
        self.jobs.upsert(replace(job, enabled=True))

    def _required(self, job_id: str) -> ScheduledJob:
        job = self.jobs.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job


class _TerminalRace:
    def __init__(self) -> None:
        self.barrier = threading.Barrier(2)
        self.ready = {
            "condition": threading.Event(),
            "deadline": threading.Event(),
        }
        self.permit = {
            "condition": threading.Event(),
            "deadline": threading.Event(),
        }
        self.committed = {
            "condition": threading.Event(),
            "deadline": threading.Event(),
        }
        self._lock = threading.Lock()
        self.terminal_writes: list[str] = []

    def before_terminal_write(self, actor: str) -> None:
        self.barrier.wait(timeout=_SYNC_TIMEOUT_SECONDS)
        self.ready[actor].set()
        if not self.permit[actor].wait(_SYNC_TIMEOUT_SECONDS):
            raise AssertionError(f"timed out waiting to release {actor} terminal write")

    def after_terminal_write(self, actor: str) -> None:
        with self._lock:
            self.terminal_writes.append(actor)
        self.committed[actor].set()


class _GatedScheduler(_DirectScheduler):
    def __init__(self, actor: str, jobs: ScheduledJobStore, race: _TerminalRace) -> None:
        super().__init__(jobs)
        self.actor = actor
        self.race = race

    def upsert(self, job: ScheduledJob) -> None:
        is_terminal_monitor_write = (
            job.job_id == _MONITOR_ID
            and not job.enabled
            and MonitorUntilConditionService.META_KEY in job.payload
        )
        if is_terminal_monitor_write:
            self.race.before_terminal_write(self.actor)
            super().upsert(job)
            self.race.after_terminal_write(self.actor)
            return
        super().upsert(job)


def _dt(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, 28, hour, minute, tzinfo=UTC)


def _job() -> ScheduledJob:
    return ScheduledJob(
        job_id=_MONITOR_ID,
        action_id="research.monitor.tick",
        trigger_kind=TriggerKind.INTERVAL,
        trigger={"minutes": 5},
        payload={"series_id": "worker59-series"},
        enabled=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_seconds=60,
    )


def _initialize_monitor(db_path: Path) -> None:
    store = SQLiteStore(db_path)
    store.initialize()
    jobs = ScheduledJobStore(store)
    service = MonitorUntilConditionService(
        scheduler=_DirectScheduler(jobs),  # type: ignore[arg-type]
        jobs=jobs,
        clock=_FakeClock(_dt(17)),
    )
    service.register(_job(), deadline_at=_dt(18))


def _race_once(
    db_path: Path,
    *,
    commit_order: tuple[str, str],
) -> tuple[dict[str, object], dict[str, object], tuple[str, ...]]:
    _initialize_monitor(db_path)
    race = _TerminalRace()
    condition_jobs = ScheduledJobStore(SQLiteStore(db_path))
    deadline_jobs = ScheduledJobStore(SQLiteStore(db_path))
    condition_service = MonitorUntilConditionService(
        scheduler=_GatedScheduler("condition", condition_jobs, race),  # type: ignore[arg-type]
        jobs=condition_jobs,
        clock=_FakeClock(_dt(18)),
    )
    deadline_service = MonitorUntilConditionService(
        scheduler=_GatedScheduler("deadline", deadline_jobs, race),  # type: ignore[arg-type]
        jobs=deadline_jobs,
        clock=_FakeClock(_dt(18)),
    )

    results: dict[str, object] = {}
    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def condition_actor() -> None:
        try:
            results["condition"] = condition_service.record_condition(
                _MONITOR_ID,
                matched=True,
                observed_at=_dt(18),
            )
        except BaseException as exc:  # noqa: BLE001 - QA thread must surface every failure
            with errors_lock:
                errors.append(exc)

    def deadline_actor() -> None:
        try:
            deadline_service.deadline_action_handler({"schedule_id": _MONITOR_ID})
            results["deadline"] = deadline_service.status(_MONITOR_ID)
        except BaseException as exc:  # noqa: BLE001 - QA thread must surface every failure
            with errors_lock:
                errors.append(exc)

    threads = (
        threading.Thread(target=condition_actor, name="worker59-condition"),
        threading.Thread(target=deadline_actor, name="worker59-deadline"),
    )
    for thread in threads:
        thread.start()

    for actor in ("condition", "deadline"):
        if not race.ready[actor].wait(_SYNC_TIMEOUT_SECONDS):
            raise AssertionError(f"{actor} did not reach the deterministic terminal barrier")

    for actor in commit_order:
        race.permit[actor].set()
        if not race.committed[actor].wait(_SYNC_TIMEOUT_SECONDS):
            raise AssertionError(f"{actor} terminal write did not commit")

    for thread in threads:
        thread.join(_SYNC_TIMEOUT_SECONDS)
        if thread.is_alive():
            raise AssertionError(f"thread did not finish: {thread.name}")
    if errors:
        raise AssertionError("concurrent monitor actor failed") from errors[0]

    restarted_jobs = ScheduledJobStore(SQLiteStore(db_path))
    restarted_service = MonitorUntilConditionService(
        scheduler=_DirectScheduler(restarted_jobs),  # type: ignore[arg-type]
        jobs=restarted_jobs,
        clock=_FakeClock(_dt(18)),
    )
    status = restarted_service.status(_MONITOR_ID)
    report = restarted_service.render_status_text(status)
    durable_job = restarted_jobs.get(_MONITOR_ID)
    assert durable_job is not None

    snapshot = {
        "stop_reason": status.stop_reason,
        "condition_state": status.condition_state,
        "stopped_at": status.stopped_at,
        "last_observed_at": status.last_observed_at,
        "enabled": status.enabled,
        "deadline_guard_present": restarted_jobs.get(f"{_MONITOR_ID}::deadline") is not None,
        "report": report,
    }

    restarted_service.deadline_action_handler({"schedule_id": _MONITOR_ID})
    replayed = restarted_service.record_condition(
        _MONITOR_ID,
        matched=True,
        observed_at=_dt(18),
    )
    replay_snapshot = {
        "stop_reason": replayed.stop_reason,
        "condition_state": replayed.condition_state,
        "stopped_at": replayed.stopped_at,
        "last_observed_at": replayed.last_observed_at,
        "enabled": replayed.enabled,
        "deadline_guard_present": restarted_jobs.get(f"{_MONITOR_ID}::deadline") is not None,
        "report": restarted_service.render_status_text(replayed),
    }
    return snapshot, replay_snapshot, tuple(race.terminal_writes)


def test_exact_instant_condition_deadline_race_has_one_canonical_outcome(
    tmp_path: Path,
) -> None:
    condition_then_deadline, replay_a, writes_a = _race_once(
        tmp_path / "condition-then-deadline.db",
        commit_order=("condition", "deadline"),
    )
    deadline_then_condition, replay_b, writes_b = _race_once(
        tmp_path / "deadline-then-condition.db",
        commit_order=("deadline", "condition"),
    )

    expected = {
        "stop_reason": MonitorStopReason.DEADLINE_REACHED,
        "condition_state": MonitorConditionState.MATCHED,
        "stopped_at": _dt(18),
        "last_observed_at": _dt(18),
        "enabled": False,
        "deadline_guard_present": False,
        "report": (
            "Monitoring status\n"
            f"Schedule: {_MONITOR_ID}\n"
            "State: stopped\n"
            "Condition: matched\n"
            "Deadline: 2026-08-28T18:00:00+00:00\n"
            "Stopped because: deadline reached. Condition observations at the exact deadline "
            "are resolved as deadline.\n"
            "Stopped at: 2026-08-28T18:00:00+00:00\n"
        ),
    }

    assert condition_then_deadline == expected
    assert deadline_then_condition == expected
    assert condition_then_deadline == deadline_then_condition
    assert replay_a == expected
    assert replay_b == expected
    assert writes_a == ("condition",)
    assert writes_b == ("deadline",)
