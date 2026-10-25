from __future__ import annotations

from dataclasses import replace

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.scheduler import ScheduledJob, ScheduledJobStore, TriggerKind
from nika_core.scheduler.apscheduler_adapter import APSchedulerAdapter


def _dated_job(*, enabled: bool = True) -> ScheduledJob:
    return ScheduledJob(
        job_id="connectivity-activation",
        action_id="runtime.resume_after_connectivity",
        trigger_kind=TriggerKind.DATE,
        trigger={"run_date": "2035-01-02T10:00:00+00:00"},
        payload={"operation_id": "older-operation"},
        enabled=enabled,
    )


@pytest.mark.parametrize("started", [False, True])
def test_persisted_activation_preserves_durable_successor(tmp_path, started) -> None:
    store = SQLiteStore(tmp_path / "Ніка Activation Authority" / "nika core.db")
    store.initialize()
    jobs = ScheduledJobStore(store)
    audit = AuditLog(store)
    adapter = APSchedulerAdapter(jobs, lambda _: lambda payload: None, audit=audit)
    old_job = _dated_job()
    successor = replace(
        old_job,
        action_id="runtime.new_action",
        trigger={"run_date": "2035-01-03T10:00:00+00:00"},
        payload={"operation_id": "successor-operation"},
    )
    try:
        if started:
            adapter.start()
        jobs.upsert(successor)
        adapter.activate_persisted(old_job)

        assert jobs.get(old_job.job_id) == successor
        runtime = adapter._scheduler.get_job(old_job.job_id)
        if started:
            assert runtime is not None
            assert runtime.args == (old_job.job_id, successor)
        else:
            assert runtime is None
        assert audit.list_for(
            entity_type="scheduled_job", entity_id=old_job.job_id
        ) == ()
    finally:
        if started:
            adapter.shutdown()


def test_persisted_activation_reloads_reassignment_during_runtime_sync(
    tmp_path, monkeypatch
) -> None:
    store = SQLiteStore(tmp_path / "Ніка Scheduler Reassignment" / "nika core.db")
    store.initialize()
    jobs = ScheduledJobStore(store)
    adapter = APSchedulerAdapter(jobs, lambda _: lambda payload: None)
    old_job = _dated_job()
    jobs.upsert(old_job)
    successor = replace(
        old_job,
        action_id="runtime.new_action",
        trigger={"run_date": "2035-01-03T10:00:00+00:00"},
    )
    adapter.start()
    try:
        original_sync = adapter._sync_runtime_job

        def replace_before_sync(job_id):
            jobs.upsert(successor)
            return original_sync(job_id)

        monkeypatch.setattr(adapter, "_sync_runtime_job", replace_before_sync)
        adapter.activate_persisted(old_job)

        assert jobs.get(old_job.job_id) == successor
        runtime = adapter._scheduler.get_job(old_job.job_id)
        assert runtime is not None
        assert runtime.args == (old_job.job_id, successor)
    finally:
        adapter.shutdown()


def test_persisted_activation_does_not_reenable_or_install_disabled_successor(
    tmp_path,
) -> None:
    store = SQLiteStore(tmp_path / "Ніка Disabled Successor" / "nika core.db")
    store.initialize()
    jobs = ScheduledJobStore(store)
    adapter = APSchedulerAdapter(jobs, lambda _: lambda payload: None)
    old_job = _dated_job()
    disabled = replace(old_job, action_id="runtime.new_action", enabled=False)
    adapter.start()
    try:
        jobs.upsert(disabled)
        adapter.activate_persisted(old_job)

        assert jobs.get(old_job.job_id) == disabled
        assert adapter._scheduler.get_job(old_job.job_id) is None
    finally:
        adapter.shutdown()
