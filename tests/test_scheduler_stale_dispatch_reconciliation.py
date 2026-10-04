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
        assert current is None if mutation == "delete" else current is not None
        if current is not None:
            assert current.enabled is False
        resolver.assert_not_called()
    finally:
        adapter.shutdown()


def test_stale_occurrence_cannot_replace_already_installed_successor(
    tmp_path: Path,
) -> None:
    jobs, adapter, resolver, old = _started(tmp_path)
    successor = replace(old, payload={"generation": "current"})
    try:
        adapter.upsert(successor)
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
