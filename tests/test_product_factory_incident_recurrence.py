from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from threading import Event, Lock
from typing import get_type_hints

import pytest
import test_product_factory_incidents as baseline

from nika_core.product_factory_incident_contracts import (
    INCIDENT_LIFECYCLE_SCHEMA,
    INCIDENT_LIFECYCLE_SCHEMA_V1,
    IncidentState,
    ProductIncidentError,
    ReleaseDisposition,
)
from nika_core.product_factory_incident_persistence import (
    dump_incident_snapshot,
    load_incident_snapshot,
)
from nika_core.product_factory_incidents import (
    IncidentRepairReleaseCoordinator,
    TrustedReviewAuthority,
)


class _RacingFingerprintMap(dict[str, str]):
    """Force two unsynchronized callers to capture the same pre-write lookup value."""

    def __init__(self, values: dict[str, str]) -> None:
        super().__init__(values)
        self._calls = 0
        self._guard = Lock()
        self._second_lookup = Event()

    def get(self, key: str, default: str | None = None) -> str | None:
        captured = super().get(key, default)
        with self._guard:
            self._calls += 1
            call = self._calls
            if call == 2:
                self._second_lookup.set()
        if call == 1:
            self._second_lookup.wait(timeout=0.2)
        return captured


def _rolled_back_first_occurrence():
    coordinator, _, item, review = baseline.reviewed()
    deployment = baseline.deployments(ReleaseDisposition.ROLLED_BACK)
    evidence = baseline.release(
        ReleaseDisposition.ROLLED_BACK,
        item=item,
        health_refs=("health://candidate/bad", "rollback://verify"),
        restored=baseline.OLD_SHA,
    )
    terminal = coordinator.record_release(evidence, deployment)
    assert terminal.state is IncidentState.ROLLED_BACK
    return coordinator, review, deployment, terminal


def _second_occurrence(
    coordinator: IncidentRepairReleaseCoordinator,
    *,
    minutes: int = 4,
):
    repeat_trigger = replace(
        baseline.trigger(),
        approval_ref="approval://incident/repeat-2",
        observed_at=baseline.NOW + timedelta(minutes=minutes),
    )
    return coordinator.open_incident(
        "incident-2",
        repeat_trigger,
        baseline.operations().snapshot(),
    )


def test_persistence_review_authority_annotation_matches_runtime_contract() -> None:
    hints = get_type_hints(load_incident_snapshot)
    assert hints["review_authorities"] == tuple[TrustedReviewAuthority, ...]


def test_active_duplicate_is_still_idempotent() -> None:
    coordinator = IncidentRepairReleaseCoordinator("project-a")
    first = coordinator.open_incident(
        "incident-1",
        baseline.trigger(),
        baseline.operations().snapshot(),
    )
    later = replace(
        baseline.trigger(),
        approval_ref="approval://incident/retry",
        observed_at=baseline.NOW + timedelta(hours=1),
    )
    assert coordinator.open_incident(
        "incident-retry",
        later,
        baseline.operations().snapshot(),
    ) == first
    assert len(coordinator.list_incidents()) == 1


def test_terminal_stale_retry_is_suppressed_but_later_repeat_is_isolated() -> None:
    coordinator, _, _, terminal = _rolled_back_first_occurrence()
    terminal_at = terminal.release_events[-1].observed_at

    stale = replace(
        baseline.trigger(),
        approval_ref="approval://incident/stale-retry",
        observed_at=terminal_at,
    )
    assert coordinator.open_incident(
        "incident-stale-retry",
        stale,
        baseline.operations().snapshot(),
    ).incident_id == "incident-1"

    repeat = _second_occurrence(coordinator)
    assert repeat.incident_id == "incident-2"
    assert repeat.state is IncidentState.OPEN
    assert repeat.trigger.fingerprint == terminal.trigger.fingerprint
    assert len(coordinator.list_incidents()) == 2
    assert dict(coordinator.snapshot().fingerprint_index)[repeat.trigger.fingerprint] == "incident-2"


def test_concurrent_later_repeat_creates_exactly_one_new_occurrence() -> None:
    coordinator, review, deployment, terminal = _rolled_back_first_occurrence()
    coordinator._fingerprints = _RacingFingerprintMap(dict(coordinator._fingerprints))
    repeat_trigger = replace(
        baseline.trigger(),
        approval_ref="approval://incident/concurrent-repeat",
        observed_at=baseline.NOW + timedelta(minutes=4),
    )
    operations = baseline.operations().snapshot()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            coordinator.open_incident,
            "incident-concurrent-a",
            repeat_trigger,
            operations,
        )
        second = pool.submit(
            coordinator.open_incident,
            "incident-concurrent-b",
            repeat_trigger,
            operations,
        )
        results = (first.result(timeout=2), second.result(timeout=2))

    assert len({item.incident_id for item in results}) == 1
    assert len(coordinator.list_incidents()) == 2
    latest = results[0]
    assert latest.state is IncidentState.OPEN
    assert latest.trigger.fingerprint == terminal.trigger.fingerprint
    assert dict(coordinator.snapshot().fingerprint_index)[latest.trigger.fingerprint] == latest.incident_id

    restarted = IncidentRepairReleaseCoordinator("project-a")
    restarted.restore(
        coordinator.snapshot(),
        deployments=deployment,
        review_authorities=(review,),
    )
    assert restarted.snapshot() == coordinator.snapshot()


def test_repeat_occurrence_remains_active_dedup_target() -> None:
    coordinator, _, _, _ = _rolled_back_first_occurrence()
    repeat = _second_occurrence(coordinator)
    later = replace(
        repeat.trigger,
        approval_ref="approval://incident/repeat-retry",
        observed_at=baseline.NOW + timedelta(minutes=20),
    )
    result = coordinator.open_incident(
        "incident-3",
        later,
        baseline.operations().snapshot(),
    )
    assert result.incident_id == "incident-2"
    assert len(coordinator.list_incidents()) == 2


def test_repeat_family_survives_restart_and_preserves_latest_dedup_target() -> None:
    coordinator, review, deployment, _ = _rolled_back_first_occurrence()
    repeat = _second_occurrence(coordinator)
    payload = dump_incident_snapshot(coordinator.snapshot())

    loaded = load_incident_snapshot(
        payload,
        deployments=deployment,
        review_authorities=(review,),
    )
    restarted = IncidentRepairReleaseCoordinator("project-a")
    restarted.restore(
        loaded,
        deployments=deployment,
        review_authorities=(review,),
    )

    assert restarted.get("incident-1").state is IncidentState.ROLLED_BACK
    assert restarted.get("incident-2") == repeat
    assert restarted.snapshot().schema == INCIDENT_LIFECYCLE_SCHEMA
    assert dict(restarted.snapshot().fingerprint_index)[repeat.trigger.fingerprint] == "incident-2"

    retry = replace(
        repeat.trigger,
        approval_ref="approval://incident/restarted-retry",
        observed_at=baseline.NOW + timedelta(hours=2),
    )
    assert restarted.open_incident(
        "incident-after-restart",
        retry,
        baseline.operations().snapshot(),
    ).incident_id == "incident-2"


def test_repeat_snapshot_rejects_index_rewind_to_old_terminal_incident() -> None:
    coordinator, review, deployment, _ = _rolled_back_first_occurrence()
    repeat = _second_occurrence(coordinator)
    payload = json.loads(dump_incident_snapshot(coordinator.snapshot()))
    fingerprint = repeat.trigger.fingerprint
    payload["fingerprint_index"] = [[fingerprint, "incident-1"]]

    with pytest.raises(ProductIncidentError, match="latest occurrence"):
        load_incident_snapshot(
            json.dumps(payload),
            deployments=deployment,
            review_authorities=(review,),
        )


def test_repeat_snapshot_rejects_occurrence_before_prior_terminal_release() -> None:
    coordinator, review, deployment, terminal = _rolled_back_first_occurrence()
    _second_occurrence(coordinator)
    payload = json.loads(dump_incident_snapshot(coordinator.snapshot()))
    terminal_at = terminal.release_events[-1].observed_at
    payload["incidents"][1]["trigger"]["observed_at"] = (
        terminal_at - timedelta(seconds=1)
    ).isoformat()

    with pytest.raises(ProductIncidentError, match="after prior terminal release"):
        load_incident_snapshot(
            json.dumps(payload),
            deployments=deployment,
            review_authorities=(review,),
        )


def test_v1_snapshot_restores_with_stable_fingerprint_and_lazily_writes_v2() -> None:
    coordinator = IncidentRepairReleaseCoordinator("project-a")
    first = coordinator.open_incident(
        "incident-1",
        baseline.trigger(),
        baseline.operations().snapshot(),
    )
    legacy = replace(coordinator.snapshot(), schema=INCIDENT_LIFECYCLE_SCHEMA_V1)
    payload = dump_incident_snapshot(legacy)
    loaded = load_incident_snapshot(payload)

    assert loaded.schema == INCIDENT_LIFECYCLE_SCHEMA_V1
    assert dict(loaded.fingerprint_index)[first.trigger.fingerprint] == "incident-1"

    restarted = IncidentRepairReleaseCoordinator("project-a")
    restarted.restore(loaded)
    upgraded = restarted.snapshot()
    assert upgraded.schema == INCIDENT_LIFECYCLE_SCHEMA
    assert upgraded.incidents[0].trigger.fingerprint == first.trigger.fingerprint
    assert upgraded.fingerprint_index == legacy.fingerprint_index
