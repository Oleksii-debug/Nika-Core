from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.scheduler import APSchedulerAdapter, ScheduledJob, ScheduledJobStore, TriggerKind


def _started(tmp_path: Path) -> tuple[ScheduledJobStore, APSchedulerAdapter, Mock, ScheduledJob]:
    sqlite = SQLiteStore(tmp_path / "Ніка stale dispatch" / "jobs.sqlite3")
    sqlite.initialize()
    jobs = ScheduledJobStore(sqlite)
    resolver = Mock(return_value=Mock())
    old = ScheduledJob(
        job_id="stale-dispatch",
        action_id="runtime.old",
        trigger_kind=TriggerKind.DATE,
        trigger={"run_date": "2035-01-02T10:00:00+00:00"},
        payload={"generation": "old"},
    )
    jobs.upsert(old)
    adapter = APSchedulerAdapter(jobs, resolver)
    adapter.start()
    runtime = adapter._scheduler.get_job(old.job_id)
    assert runtime is not None and runtime.args == (old.job_id, old)
    return jobs, adapter, resolver, old


def test_stale_dispatch_reinstalls_durable_successor_without_running_old_handler(
    tmp_path: Path,
) -> None:
    jobs, adapter, resolver, old = _started(tmp_path)
    successor = replace(
        old,
        action_id="runtime.successor",
        trigger={"run_date": "2035-01-03T10:00:00+00:00"},
        payload={"generation": "successor"},
    )
    try:
        # Simulate another process committing while this adapter still holds
        # the earlier runtime occurrence. Do not call adapter.upsert().
        jobs.upsert(successor)
        adapter._dispatch(old.job_id, old)
        runtime = adapter._scheduler.get_job(old.job_id)
        assert runtime is not None and runtime.args == (old.job_id, successor)
        assert jobs.get(old.job_id) == successor
        resolver.assert_not_called()
    finally:
        adapter.shutdown()


@pytest.mark.parametrize("mutation", ["disable", "delete"])
def test_stale_dispatch_removes_obsolete_runtime_after_durable_mutation(
    tmp_path: Path, mutation: str
) -> None:
    jobs, adapter, resolver, old = _started(tmp_path)
    try:
        if mutation == "disable":
            assert jobs.set_enabled(old.job_id, False)
        else:
            assert jobs.delete(old.job_id)
        adapter._dispatch(old.job_id, old)
        assert adapter._scheduler.get_job(old.job_id) is None
        current = jobs.get(old.job_id)
        if mutation == "delete":
            assert current is None
        else:
            assert current is not None
            assert current.enabled is False
        resolver.assert_not_called()
    finally:
        adapter.shutdown()


def test_stale_occurrence_cannot_replace_already_installed_successor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs, adapter, resolver, old = _started(tmp_path)
    successor = replace(old, payload={"generation": "current"})
    try:
        adapter.upsert(successor)

        def unexpected_reinstall(_job: ScheduledJob) -> None:
            raise AssertionError("already-installed successor must retain its trigger")

        monkeypatch.setattr(adapter, "_install", unexpected_reinstall)
        adapter._dispatch(old.job_id, old)
        runtime = adapter._scheduler.get_job(old.job_id)
        assert runtime is not None and runtime.args == (old.job_id, successor)
        assert jobs.get(old.job_id) == successor
        resolver.assert_not_called()
    finally:
        adapter.shutdown()


def test_stale_dispatch_after_shutdown_does_not_restart_scheduler(
    tmp_path: Path,
) -> None:
    jobs, adapter, resolver, old = _started(tmp_path)
    adapter.shutdown()
    successor = replace(old, payload={"generation": "current"})
    jobs.upsert(successor)

    adapter._dispatch(old.job_id, old)

    assert jobs.get(old.job_id) == successor
    assert adapter._scheduler.get_job(old.job_id) is None
    resolver.assert_not_called()


def test_stale_date_occurrence_preserves_successor_interval_cadence(
    tmp_path: Path,
) -> None:
    jobs, adapter, resolver, old = _started(tmp_path)
    successor = replace(
        old,
        trigger_kind=TriggerKind.INTERVAL,
        trigger={"seconds": 120},
        payload={"generation": "interval-successor"},
    )
    try:
        adapter.upsert(successor)
        initial = adapter._scheduler.get_job(old.job_id)
        assert initial is not None
        next_run = initial.next_run_time

        adapter._dispatch(old.job_id, old)

        runtime = adapter._scheduler.get_job(old.job_id)
        assert runtime is not None and runtime.args == (old.job_id, successor)
        assert runtime.next_run_time == next_run
        assert jobs.get(old.job_id) == successor
        resolver.assert_not_called()
    finally:
        adapter.shutdown()



def test_reassignment_between_runtime_check_and_last_durable_read_is_reconciled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs, adapter, resolver, old = _started(tmp_path)
    successor = replace(old, payload={"generation": "successor"})
    later = replace(
        successor,
        trigger={"run_date": "2035-01-04T10:00:00+00:00"},
        payload={"generation": "later"},
    )
    try:
        adapter.upsert(successor)
        original_get = jobs.get
        reads = 0

        def replace_on_second_get(job_id: str) -> ScheduledJob | None:
            nonlocal reads
            reads += 1
            if reads == 2:
                jobs.upsert(later)
            return original_get(job_id)

        monkeypatch.setattr(jobs, "get", replace_on_second_get)
        adapter._dispatch(old.job_id, old)

        assert jobs.get(old.job_id) == later
        runtime = adapter._scheduler.get_job(old.job_id)
        assert runtime is not None and runtime.args == (old.job_id, later)
        resolver.assert_not_called()
    finally:
        adapter.shutdown()
