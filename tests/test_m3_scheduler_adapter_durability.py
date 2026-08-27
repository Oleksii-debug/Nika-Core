from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.scheduler import APSchedulerAdapter, ScheduledJob, ScheduledJobStore, TriggerKind


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    return store


def _job(
    *,
    job_id: str,
    trigger_kind: TriggerKind,
    trigger: dict[str, object],
    enabled: bool = True,
) -> ScheduledJob:
    return ScheduledJob(
        job_id=job_id,
        action_id="test.action",
        trigger_kind=trigger_kind,
        trigger=trigger,
        enabled=enabled,
    )


@pytest.mark.parametrize("enabled", [True, False])
def test_adapter_rejects_invalid_cron_before_durable_upsert(
    tmp_path: Path,
    enabled: bool,
) -> None:
    jobs = ScheduledJobStore(_store(tmp_path))
    adapter = APSchedulerAdapter(jobs, lambda _action_id: lambda _payload: None)
    invalid = _job(
        job_id="invalid-cron",
        trigger_kind=TriggerKind.CRON,
        trigger={"hour": 25, "timezone": "UTC"},
        enabled=enabled,
    )

    with pytest.raises(ValueError):
        adapter.upsert(invalid)

    assert jobs.get("invalid-cron") is None


def test_adapter_rejects_unknown_timezone_before_durable_upsert(tmp_path: Path) -> None:
    jobs = ScheduledJobStore(_store(tmp_path))
    adapter = APSchedulerAdapter(jobs, lambda _action_id: lambda _payload: None)
    invalid = _job(
        job_id="invalid-zone",
        trigger_kind=TriggerKind.CRON,
        trigger={"hour": 1, "timezone": "Nika/Definitely-Unknown-Timezone"},
    )

    with pytest.raises((ValueError, KeyError)):
        adapter.upsert(invalid)

    assert jobs.get("invalid-zone") is None


def test_resume_preflights_legacy_invalid_trigger_before_enabling(tmp_path: Path) -> None:
    store = _store(tmp_path)
    jobs = ScheduledJobStore(store)
    legacy_invalid = _job(
        job_id="legacy-invalid",
        trigger_kind=TriggerKind.CRON,
        trigger={"hour": 25, "timezone": "UTC"},
        enabled=False,
    )
    jobs.upsert(legacy_invalid)
    adapter = APSchedulerAdapter(jobs, lambda _action_id: lambda _payload: None)

    with pytest.raises(ValueError):
        adapter.resume("legacy-invalid")

    restored = jobs.get("legacy-invalid")
    assert restored is not None
    assert restored.enabled is False


def test_pause_audit_records_durable_disabled_state(tmp_path: Path) -> None:
    store = _store(tmp_path)
    jobs = ScheduledJobStore(store)
    audit = AuditLog(store)
    adapter = APSchedulerAdapter(
        jobs,
        lambda _action_id: lambda _payload: None,
        audit=audit,
    )
    adapter.upsert(
        _job(
            job_id="pause-me",
            trigger_kind=TriggerKind.INTERVAL,
            trigger={"minutes": 5, "timezone": "UTC"},
        )
    )

    adapter.pause("pause-me")

    restored = jobs.get("pause-me")
    assert restored is not None
    assert restored.enabled is False
    events = audit.list_for(entity_type="scheduled_job", entity_id="pause-me")
    assert events[-1].event_type == "scheduler.job_paused"
    assert events[-1].payload["enabled"] is False
