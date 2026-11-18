from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event
from unittest.mock import Mock

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.scheduler import APSchedulerAdapter, ScheduledJob, ScheduledJobStore, TriggerKind


def _running(tmp_path: Path) -> tuple[ScheduledJobStore, APSchedulerAdapter, Mock, ScheduledJob]:
    sqlite = SQLiteStore(tmp_path / "Ніка shutdown concurrency" / "jobs.sqlite3")
    sqlite.initialize()
    jobs = ScheduledJobStore(sqlite)
    resolver = Mock(return_value=Mock())
    job = ScheduledJob(
        job_id="shutdown-race",
        action_id="scheduler.old",
        trigger_kind=TriggerKind.DATE,
        trigger={"run_date": "2035-01-02T10:00:00+00:00"},
        payload={"generation": "old"},
    )
    jobs.upsert(job)
    adapter = APSchedulerAdapter(jobs, resolver)
    adapter.start()
    return jobs, adapter, resolver, job


def test_delayed_reconciliation_cannot_install_into_stopped_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs, adapter, resolver, old = _running(tmp_path)
    successor = replace(
        old,
        action_id="scheduler.successor",
        trigger={"run_date": "2035-01-03T10:00:00+00:00"},
        payload={"generation": "successor"},
    )
    jobs.upsert(successor)
    entered = Event()
    release = Event()
    original_sync = adapter._sync_runtime_job

    def delayed_sync(job_id: str) -> ScheduledJob | None:
        # The caller has passed its _started check, but shutdown can win
        # before the synchronizer acquires its internal runtime lock.
        entered.set()
        if not release.wait(timeout=5):
            raise AssertionError("delayed reconciliation was not released")
        return original_sync(job_id)

    monkeypatch.setattr(adapter, "_sync_runtime_job", delayed_sync)
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(adapter.activate_persisted, old)
        try:
            assert entered.wait(timeout=5)
            adapter.shutdown(wait=False)
        finally:
            release.set()
        pending.result(timeout=5)

    assert not adapter._started
    assert adapter._scheduler.get_job(old.job_id) is None
    assert jobs.get(old.job_id) == successor
    resolver.assert_not_called()

    # Restart is a separate explicit operation; it must rehydrate the
    # durable successor rather than recovering an old pending trigger.
    adapter.start()
    try:
        runtime = adapter._scheduler.get_job(old.job_id)
        assert runtime is not None and runtime.args == (old.job_id, successor)
    finally:
        adapter.shutdown(wait=False)


def test_shutdown_serializes_with_inflight_install_and_retires_that_scheduler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs, adapter, resolver, job = _running(tmp_path)
    entered = Event()
    release = Event()
    shutdown_entered = Event()
    original_install = adapter._install

    def delayed_install(snapshot: ScheduledJob) -> None:
        if snapshot == job:
            entered.set()
            if not release.wait(timeout=5):
                raise AssertionError("inflight install was not released")
        original_install(snapshot)

    monkeypatch.setattr(adapter, "_install", delayed_install)

    def stop() -> None:
        shutdown_entered.set()
        adapter.shutdown(wait=False)

    with ThreadPoolExecutor(max_workers=2) as executor:
        activation = executor.submit(adapter.activate_persisted, job)
        try:
            assert entered.wait(timeout=5)
            stopping = executor.submit(stop)
            assert shutdown_entered.wait(timeout=5)
            assert not stopping.done()
        finally:
            release.set()
        activation.result(timeout=5)
        stopping.result(timeout=5)

    assert jobs.get(job.job_id) == job
    assert not adapter._started
    assert adapter._scheduler.get_job(job.job_id) is None
    resolver.assert_not_called()


def test_waiting_shutdown_releases_sync_lock_before_waiting_for_handlers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, adapter, resolver, job = _running(tmp_path)
    retiring = adapter._scheduler
    original_shutdown = retiring.shutdown
    observed: list[bool] = []

    def shutdown_probe(*, wait: bool) -> None:
        assert wait is True
        assert not adapter._started
        assert adapter._scheduler is not retiring

        def can_acquire() -> bool:
            acquired = adapter._runtime_sync_lock.acquire(blocking=False)
            if acquired:
                adapter._runtime_sync_lock.release()
            return acquired

        with ThreadPoolExecutor(max_workers=1) as executor:
            observed.append(executor.submit(can_acquire).result(timeout=5))
        original_shutdown(wait=wait)

    monkeypatch.setattr(retiring, "shutdown", shutdown_probe)
    adapter.shutdown(wait=True)

    assert observed == [True]
    assert adapter._scheduler.get_job(job.job_id) is None
    resolver.assert_not_called()
