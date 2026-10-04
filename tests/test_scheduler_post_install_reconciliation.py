from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.scheduler import (
    APSchedulerAdapter,
    ScheduledJob,
    ScheduledJobStore,
    TriggerKind,
)


def _setup(tmp_path: Path) -> tuple[ScheduledJobStore, APSchedulerAdapter, Mock]:
    store = SQLiteStore(tmp_path / "Ніка scheduler" / "jobs.sqlite3")
    store.initialize()
    jobs = ScheduledJobStore(store)
    resolver = Mock(return_value=Mock())
    adapter = APSchedulerAdapter(jobs, resolver)
    adapter.start()
    return jobs, adapter, resolver


def _job(*, generation: str = "old") -> ScheduledJob:
    return ScheduledJob(
        job_id="post-install-race",
        action_id="scheduler.test",
        trigger_kind=TriggerKind.DATE,
        trigger={"run_date": "2035-01-02T10:00:00+00:00"},
        payload={"generation": generation},
    )


def test_replacement_during_install_is_reconciled_without_old_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs, adapter, resolver = _setup(tmp_path)
    old = _job()
    successor = replace(old, action_id="scheduler.new", payload={"generation": "new"})
    jobs.upsert(old)
    original_install = adapter._install
    installed: list[ScheduledJob] = []

    def install_and_replace(job: ScheduledJob) -> None:
        original_install(job)
        installed.append(job)
        if job == old:
            jobs.upsert(successor)

    monkeypatch.setattr(adapter, "_install", install_and_replace)
    try:
        assert adapter._sync_runtime_job(old.job_id) == successor
        assert installed == [old, successor]
        assert jobs.get(old.job_id) == successor
        runtime = adapter._scheduler.get_job(old.job_id)
        assert runtime is not None and runtime.args == (old.job_id, successor)
        adapter._dispatch(old.job_id, old)
        resolver.assert_not_called()
    finally:
        adapter.shutdown()


@pytest.mark.parametrize("change", ["disable", "delete"])
def test_disabling_or_removing_during_install_clears_old_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    jobs, adapter, resolver = _setup(tmp_path)
    old = _job()
    jobs.upsert(old)
    original_install = adapter._install

    def install_then_remove(job: ScheduledJob) -> None:
        original_install(job)
        if change == "disable":
            jobs.upsert(replace(job, enabled=False))
        else:
            jobs.delete(job.job_id)

    monkeypatch.setattr(adapter, "_install", install_then_remove)
    try:
        assert adapter._sync_runtime_job(old.job_id) is None
        assert adapter._scheduler.get_job(old.job_id) is None
        current = jobs.get(old.job_id)
        assert current is None if change == "delete" else current is not None and not current.enabled
        resolver.assert_not_called()
    finally:
        adapter.shutdown()


def test_continuous_replacement_exhausts_bounded_retries_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs, adapter, resolver = _setup(tmp_path)
    jobs.upsert(_job())
    original_install = adapter._install
    installed: list[ScheduledJob] = []

    def replace_every_install(job: ScheduledJob) -> None:
        original_install(job)
        installed.append(job)
        jobs.upsert(replace(job, payload={"generation": str(len(installed))}))

    monkeypatch.setattr(adapter, "_install", replace_every_install)
    try:
        assert adapter._sync_runtime_job("post-install-race") is None
        assert len(installed) == 3
        assert jobs.get("post-install-race") is not None
        assert adapter._scheduler.get_job("post-install-race") is None
        resolver.assert_not_called()
    finally:
        adapter.shutdown()


def test_unchanged_snapshot_installs_normally(tmp_path: Path) -> None:
    jobs, adapter, resolver = _setup(tmp_path)
    job = _job()
    jobs.upsert(job)
    try:
        assert adapter._sync_runtime_job(job.job_id) == job
        runtime = adapter._scheduler.get_job(job.job_id)
        assert runtime is not None and runtime.args == (job.job_id, job)
        resolver.assert_not_called()
    finally:
        adapter.shutdown()
