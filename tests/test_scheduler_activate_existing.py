from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
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


def _jobs(tmp_path: Path) -> ScheduledJobStore:
    store = SQLiteStore(tmp_path / "scheduler-activation.sqlite3")
    store.initialize()
    return ScheduledJobStore(store)


def _job(*, generation: str = "a") -> ScheduledJob:
    return ScheduledJob(
        job_id="persistent-activation",
        action_id="activation.test",
        trigger_kind=TriggerKind.DATE,
        trigger={"run_date": (datetime.now(UTC) + timedelta(days=1)).isoformat()},
        payload={"generation": generation},
        misfire_grace_seconds=3600,
    )


def test_persisted_activation_never_calls_durable_upsert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = _jobs(tmp_path)
    original = _job()
    jobs.upsert(original)
    adapter = APSchedulerAdapter(jobs, lambda _action: lambda _payload: None)
    adapter._started = True
    install = Mock()
    monkeypatch.setattr(adapter, "_install", install)
    write = Mock(side_effect=AssertionError("activation must not rewrite SQLite"))
    monkeypatch.setattr(jobs, "upsert", write)

    assert adapter.activate_existing(original) is True
    assert jobs.get(original.job_id) == original
    install.assert_called_once_with(original)
    write.assert_not_called()


def test_stale_persisted_activation_cannot_overwrite_successor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = _jobs(tmp_path)
    original = _job()
    successor = replace(original, payload={"generation": "b"})
    jobs.upsert(original)
    jobs.upsert(successor)
    adapter = APSchedulerAdapter(jobs, lambda _action: lambda _payload: None)
    adapter._started = True
    install = Mock()
    monkeypatch.setattr(adapter, "_install", install)

    assert adapter.activate_existing(original) is False
    assert jobs.get(original.job_id) == successor
    install.assert_not_called()


def test_replacement_during_install_does_not_rewrite_durable_successor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = _jobs(tmp_path)
    original = _job()
    successor = replace(original, payload={"generation": "b"})
    jobs.upsert(original)
    resolver = Mock(return_value=Mock())
    adapter = APSchedulerAdapter(jobs, resolver)
    adapter._started = True
    installed: list[ScheduledJob] = []

    def replace_during_install(job: ScheduledJob) -> None:
        installed.append(job)
        jobs.upsert(successor)

    monkeypatch.setattr(adapter, "_install", replace_during_install)
    assert adapter.activate_existing(original) is False
    assert installed == [original]
    assert jobs.get(original.job_id) == successor

    # The old occurrence may already be queued locally; its exact dispatch
    # snapshot must never reach the resolver after a durable replacement.
    adapter._dispatch(original.job_id, original)
    resolver.assert_not_called()


def test_disabled_and_missing_snapshots_are_never_activated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = _jobs(tmp_path)
    original = _job()
    adapter = APSchedulerAdapter(jobs, lambda _action: lambda _payload: None)
    adapter._started = True
    install = Mock()
    monkeypatch.setattr(adapter, "_install", install)

    assert adapter.activate_existing(original) is False
    jobs.upsert(original)
    jobs.set_enabled(original.job_id, False)
    assert adapter.activate_existing(original) is False
    install.assert_not_called()


def test_unstarted_adapter_defers_activation_to_start_without_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    jobs = _jobs(tmp_path)
    original = _job()
    jobs.upsert(original)
    adapter = APSchedulerAdapter(jobs, lambda _action: lambda _payload: None)
    install = Mock()
    monkeypatch.setattr(adapter, "_install", install)

    assert adapter.activate_existing(original) is True
    install.assert_not_called()
    assert jobs.get(original.job_id) == original


def test_activation_rejects_noncanonical_expected_job(tmp_path: Path) -> None:
    adapter = APSchedulerAdapter(_jobs(tmp_path), lambda _action: lambda _payload: None)
    with pytest.raises(TypeError, match="exact ScheduledJob"):
        adapter.activate_existing("persistent-activation")  # type: ignore[arg-type]
