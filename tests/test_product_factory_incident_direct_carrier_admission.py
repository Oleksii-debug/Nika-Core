from __future__ import annotations

from datetime import UTC, datetime

import pytest
import test_product_factory_incidents as baseline

from nika_core.product_factory_incident_contracts import (
    INCIDENT_LIFECYCLE_SCHEMA,
    IncidentKind,
    IncidentLifecycleSnapshot,
    IncidentRecord,
    IncidentSeverity,
    IncidentState,
    IncidentTrigger,
    ProductIncidentError,
    RepairCandidateEvidence,
)
from nika_core.product_factory_incident_persistence import (
    dump_incident_snapshot,
    load_incident_snapshot,
)
from nika_core.product_factory_incidents import IncidentRepairReleaseCoordinator


SHA = "1" * 40
DIGEST = "2" * 64
NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


class _BehavioralText(str):
    events: list[str]

    def strip(self, *args: object, **kwargs: object) -> str:
        self.events.append("strip")
        return "forged-nonempty"

    def encode(self, *args: object, **kwargs: object) -> bytes:
        self.events.append("encode")
        return b"forged"


def _behavioral_text(value: str, events: list[str]) -> _BehavioralText:
    text = _BehavioralText(value)
    text.events = events
    return text


def _trigger() -> IncidentTrigger:
    return IncidentTrigger(
        "project-a",
        "api",
        "prod",
        SHA,
        IncidentKind.HEALTH,
        IncidentSeverity.HIGH,
        ("health://degraded",),
        "approval://incident",
        NOW,
    )


def test_snapshot_rejects_behavioral_project_text_before_hooks() -> None:
    events: list[str] = []
    project_id = _behavioral_text("", events)

    with pytest.raises(ProductIncidentError, match="project.*must be text"):
        IncidentLifecycleSnapshot(INCIDENT_LIFECYCLE_SCHEMA, project_id, (), ())

    assert events == []


def test_trigger_rejects_behavioral_identity_before_hooks() -> None:
    events: list[str] = []
    service_id = _behavioral_text("", events)

    with pytest.raises(ProductIncidentError, match="incident trigger identity must be text"):
        IncidentTrigger(
            "project-a",
            service_id,
            "prod",
            SHA,
            IncidentKind.HEALTH,
            IncidentSeverity.HIGH,
            ("health://degraded",),
            "approval://incident",
            NOW,
        )

    assert events == []


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("kind", "health", "incident kind must be an IncidentKind"),
        ("severity", "high", "incident severity must be an IncidentSeverity"),
    ),
)
def test_trigger_requires_exact_enum_carriers(
    field: str,
    value: object,
    message: str,
) -> None:
    kwargs = {
        "project_id": "project-a",
        "service_id": "api",
        "environment_id": "prod",
        "release_sha": SHA,
        "kind": IncidentKind.HEALTH,
        "severity": IncidentSeverity.HIGH,
        "evidence_refs": ("health://degraded",),
        "approval_ref": "approval://incident",
        "observed_at": NOW,
    }
    kwargs[field] = value

    with pytest.raises(ProductIncidentError, match=message):
        IncidentTrigger(**kwargs)  # type: ignore[arg-type]


def test_candidate_requires_exact_boolean_review_authority() -> None:
    with pytest.raises(ProductIncidentError, match="review_accepted must be boolean"):
        RepairCandidateEvidence(
            "candidate-1",
            "incident-1",
            "work-1",
            SHA,
            "3" * 40,
            DIGEST,
            "4" * 64,
            ("5" * 64,),
            ("provenance://candidate",),
            "review://candidate",
            1,  # type: ignore[arg-type]
            NOW,
        )


def test_incident_record_requires_exact_state_carrier() -> None:
    with pytest.raises(ProductIncidentError, match="incident state must be an IncidentState"):
        IncidentRecord(
            "incident-1",
            _trigger(),
            "open",  # type: ignore[arg-type]
        )


def test_valid_snapshot_remains_dump_load_stable() -> None:
    trigger = _trigger()
    record = IncidentRecord("incident-1", trigger, IncidentState.OPEN)
    snapshot = IncidentLifecycleSnapshot(
        INCIDENT_LIFECYCLE_SCHEMA,
        "project-a",
        (record,),
        ((trigger.fingerprint, "incident-1"),),
    )

    payload = dump_incident_snapshot(snapshot)

    assert load_incident_snapshot(payload) == snapshot
    assert dump_incident_snapshot(load_incident_snapshot(payload)) == payload

def test_coordinator_revalidates_trigger_after_post_construction_mutation() -> None:
    coordinator = IncidentRepairReleaseCoordinator("project-a")
    trigger = baseline.trigger()
    object.__setattr__(trigger, "evidence_refs", ())

    with pytest.raises(ProductIncidentError, match="incident evidence must not be empty"):
        coordinator.open_incident(
            "incident-tampered",
            trigger,
            baseline.operations().snapshot(),
        )

    assert coordinator.list_incidents() == ()


def test_returned_incident_record_does_not_alias_coordinator_state() -> None:
    coordinator = IncidentRepairReleaseCoordinator("project-a")
    saved = coordinator.open_incident(
        "incident-1",
        baseline.trigger(),
        baseline.operations().snapshot(),
    )
    original_refs = saved.trigger.evidence_refs

    object.__setattr__(saved.trigger, "evidence_refs", ())

    stored = coordinator.get("incident-1")
    assert stored.trigger.evidence_refs == original_refs
    assert coordinator.snapshot().incidents[0].trigger.evidence_refs == original_refs


def test_coordinator_revalidates_work_order_after_post_construction_mutation() -> None:
    coordinator = IncidentRepairReleaseCoordinator("project-a")
    coordinator.open_incident(
        "incident-1",
        baseline.trigger(),
        baseline.operations().snapshot(),
    )
    work = baseline.order()
    object.__setattr__(work, "allowed_paths", ())

    with pytest.raises(ProductIncidentError, match="allowed paths must not be empty"):
        coordinator.create_repair_work_order(work)

    assert coordinator.get("incident-1").work_order is None


def test_coordinator_revalidates_candidate_after_post_construction_mutation() -> None:
    coordinator, work = baseline.planned()
    item = baseline.candidate()
    proof = baseline.authority(work, item)
    object.__setattr__(item, "review_accepted", 1)

    with pytest.raises(ProductIncidentError, match="review_accepted must be boolean"):
        coordinator.record_candidate(item, proof)

    assert coordinator.get("incident-1").candidates == ()


def test_coordinator_revalidates_release_after_post_construction_mutation() -> None:
    coordinator, _, item, _ = baseline.reviewed()
    evidence = baseline.release(item=item)
    object.__setattr__(evidence, "deployment_evidence_refs", ())

    with pytest.raises(ProductIncidentError, match="deployment evidence must not be empty"):
        coordinator.record_release(evidence, baseline.deployments())

    assert coordinator.get("incident-1").release_events == ()

