from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.scheduler import APSchedulerAdapter, ScheduledJob, ScheduledJobStore, TriggerKind
from nika_core.scheduler.apscheduler_adapter import _make_trigger


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "ніка wait state" / "nika.db")
    store.initialize()
    return store


def _wait_job(*, run_at: datetime, enabled: bool = True) -> ScheduledJob:
    return ScheduledJob(
        job_id="task-1:batch-2",
        action_id="batch.resume",
        trigger_kind=TriggerKind.DATE,
        trigger={"run_date": run_at.isoformat()},
        payload={"task_id": "task-1", "next_batch": 2},
        enabled=enabled,
        coalesce=True,
        max_instances=1,
        misfire_grace_seconds=60,
    )


def _batch_resolver(calls: list[int]):
    def resolve(action_id: str):
        assert action_id == "batch.resume"

        def resume(payload: dict[str, object]) -> None:
            calls.append(int(payload["next_batch"]))

        return resume

    return resolve


def test_wait_is_durable_date_intent_and_restart_keeps_only_next_batch(tmp_path: Path) -> None:
    store = _store(tmp_path)
    jobs = ScheduledJobStore(store)
    batch_runs = [1]  # Batch 1 completed before Nika persisted the wait intent.
    deadline = datetime.now(UTC) + timedelta(minutes=30)

    first = APSchedulerAdapter(jobs, _batch_resolver(batch_runs))
    first.upsert(_wait_job(run_at=deadline))
    persisted = jobs.get("task-1:batch-2")
    assert persisted is not None
    assert persisted.trigger_kind is TriggerKind.DATE
    assert persisted.trigger == {"run_date": deadline.isoformat()}
    assert persisted.payload == {"task_id": "task-1", "next_batch": 2}
    first.start()
    assert first.has_runtime_job("task-1:batch-2")
    first.shutdown()

    restarted = APSchedulerAdapter(ScheduledJobStore(store), _batch_resolver(batch_runs))
    restarted.start()
    assert restarted.has_runtime_job("task-1:batch-2")
    assert batch_runs == [1]
    restarted.shutdown()


def test_completed_wait_is_retired_and_cannot_start_batch_two_twice(tmp_path: Path) -> None:
    store = _store(tmp_path)
    jobs = ScheduledJobStore(store)
    batch_runs = [1]
    adapter = APSchedulerAdapter(jobs, _batch_resolver(batch_runs))
    adapter.upsert(_wait_job(run_at=datetime.now(UTC) + timedelta(minutes=30)))

    adapter._dispatch("task-1:batch-2")
    assert batch_runs == [1, 2]
    assert jobs.get("task-1:batch-2") is None

    restarted = APSchedulerAdapter(ScheduledJobStore(store), _batch_resolver(batch_runs))
    restarted.start()
    assert not restarted.has_runtime_job("task-1:batch-2")
    restarted.shutdown()
    assert batch_runs == [1, 2]


def test_failed_wait_handler_keeps_intent_for_existing_misfire_recovery(tmp_path: Path) -> None:
    store = _store(tmp_path)
    jobs = ScheduledJobStore(store)

    def fail(_payload: dict[str, object]) -> None:
        raise RuntimeError("batch two did not start")

    adapter = APSchedulerAdapter(jobs, lambda action_id: fail)
    adapter.upsert(_wait_job(run_at=datetime.now(UTC) + timedelta(minutes=30)))

    with pytest.raises(RuntimeError, match="did not start"):
        adapter._dispatch("task-1:batch-2")
    assert jobs.get("task-1:batch-2") is not None


def test_pause_and_cancel_during_wait_survive_restart(tmp_path: Path) -> None:
    store = _store(tmp_path)
    jobs = ScheduledJobStore(store)
    resolver = lambda action_id: (lambda payload: None)
    first = APSchedulerAdapter(jobs, resolver)
    first.upsert(_wait_job(run_at=datetime.now(UTC) + timedelta(minutes=30)))
    first.pause("task-1:batch-2")

    paused = APSchedulerAdapter(ScheduledJobStore(store), resolver)
    paused.start()
    assert not paused.has_runtime_job("task-1:batch-2")
    paused.resume("task-1:batch-2")
    assert paused.has_runtime_job("task-1:batch-2")
    assert paused.remove("task-1:batch-2")
    paused.shutdown()

    cancelled = APSchedulerAdapter(ScheduledJobStore(store), resolver)
    cancelled.start()
    assert not cancelled.has_runtime_job("task-1:batch-2")
    cancelled.shutdown()


def test_cancel_that_wins_after_callback_queueing_does_not_run_handler(tmp_path: Path) -> None:
    store = _store(tmp_path)
    jobs = ScheduledJobStore(store)
    batch_runs = [1]
    adapter = APSchedulerAdapter(jobs, _batch_resolver(batch_runs))
    adapter.upsert(_wait_job(run_at=datetime.now(UTC) + timedelta(minutes=30)))
    assert adapter.remove("task-1:batch-2")

    adapter._dispatch("task-1:batch-2")
    assert batch_runs == [1]


def test_overdue_date_beyond_misfire_grace_is_retired_deterministically(tmp_path: Path) -> None:
    store = _store(tmp_path)
    jobs = ScheduledJobStore(store)
    adapter = APSchedulerAdapter(jobs, lambda action_id: (lambda payload: None))
    adapter.upsert(_wait_job(run_at=datetime.now(UTC) - timedelta(minutes=10)))

    adapter._on_job_missed(SimpleNamespace(job_id="task-1:batch-2"))
    assert jobs.get("task-1:batch-2") is None


def test_wait_deadline_requires_timezone_and_is_canonicalized_to_utc() -> None:
    naive = ScheduledJob(
        job_id="naive",
        action_id="batch.resume",
        trigger_kind=TriggerKind.DATE,
        trigger={"run_date": "2030-06-01T12:00:00"},
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        _make_trigger(naive)

    kyiv = ScheduledJob(
        job_id="aware",
        action_id="batch.resume",
        trigger_kind=TriggerKind.DATE,
        trigger={"run_date": "2030-06-01T12:00:00+03:00"},
    )
    trigger = _make_trigger(kyiv)
    assert trigger.run_date == datetime(2030, 6, 1, 9, 0, tzinfo=UTC)
