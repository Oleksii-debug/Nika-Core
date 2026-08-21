from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from nika_core.product_factory_incident_contracts import (
    IncidentKind,
    IncidentSeverity,
    IncidentState,
    IncidentTrigger,
    ProductIncidentError,
    ReleaseDisposition,
    ReleaseEvidence,
    RepairCandidateEvidence,
    RepairWorkOrder,
    SupplyChainAdvisory,
)
from nika_core.product_factory_incident_persistence import (
    dump_incident_snapshot,
    load_incident_snapshot,
)
from nika_core.product_factory_incidents import IncidentRepairReleaseCoordinator
from nika_core.product_factory_operations import ProductOperationsCoordinator
from nika_core.product_factory_operations_contracts import (
    DeployableService,
    ServiceObservation,
    ServiceReplica,
)

OLD_SHA = "1" * 40
NEW_SHA = "2" * 40
OTHER_SHA = "3" * 40
ARTIFACT = "a" * 64
DIFF = "b" * 64
NOW = datetime(2026, 8, 21, 10, 0, tzinfo=UTC)


def _operations(
    *,
    project_id: str = "project-a",
    service_id: str = "api",
    release_sha: str = OLD_SHA,
) -> ProductOperationsCoordinator:
    operations = ProductOperationsCoordinator(project_id)
    operations.register(
        DeployableService(
            service_id=service_id,
            project_id=project_id,
            environment_id="prod-eu",
            release_sha=release_sha,
            wave=0,
            replicas=(
                ServiceReplica(f"{service_id}-r1", "node-1"),
                ServiceReplica(f"{service_id}-r2", "node-2"),
            ),
            min_healthy_replicas=1,
        )
    )
    operations.record_observation(
        ServiceObservation(
            service_id=service_id,
            release_sha=release_sha,
            healthy_replica_ids=(f"{service_id}-r1",),
            failed_replica_ids=(f"{service_id}-r2",),
            evidence_refs=(f"health://{service_id}/degraded",),
            observed_at=NOW,
        )
    )
    return operations


def _trigger(
    *,
    project_id: str = "project-a",
    service_id: str = "api",
    release_sha: str = OLD_SHA,
) -> IncidentTrigger:
    return IncidentTrigger(
        project_id=project_id,
        service_id=service_id,
        environment_id="prod-eu",
        release_sha=release_sha,
        kind=IncidentKind.HEALTH,
        severity=IncidentSeverity.HIGH,
        evidence_refs=(f"health://{service_id}/degraded",),
        approval_ref="approval://incident/1",
        observed_at=NOW,
    )


def _work_order(
    incident_id: str = "incident-1",
    *,
    service_id: str = "api",
    evidence_refs: tuple[str, ...] | None = None,
    advisory_id: str | None = None,
    fixed_version: str | None = None,
) -> RepairWorkOrder:
    return RepairWorkOrder(
        work_order_id=f"repair:{incident_id}",
        incident_id=incident_id,
        project_id="project-a",
        service_id=service_id,
        repository_id="repo-a",
        component_id=f"component-{service_id}",
        base_release_sha=OLD_SHA,
        goal="Repair the incident without widening component ownership.",
        allowed_paths=(f"src/{service_id}.py", f"tests/test_{service_id}.py"),
        acceptance_commands=(("python", "-m", "pytest", f"tests/test_{service_id}.py"),),
        evidence_refs=evidence_refs or (f"health://{service_id}/degraded",),
        created_at=NOW + timedelta(minutes=1),
        advisory_id=advisory_id,
        target_fixed_version=fixed_version,
    )


def _candidate(
    incident_id: str = "incident-1",
    *,
    accepted: bool = True,
    result_sha: str = NEW_SHA,
    provenance: tuple[str, ...] = ("build://exact-sha",),
) -> RepairCandidateEvidence:
    return RepairCandidateEvidence(
        candidate_id=f"candidate:{incident_id}:{result_sha[:8]}",
        incident_id=incident_id,
        work_order_id=f"repair:{incident_id}",
        base_release_sha=OLD_SHA,
        result_sha=result_sha,
        artifact_digest=ARTIFACT,
        diff_digest=DIFF,
        regression_evidence_refs=("test://focused-green", "test://broad-green"),
        provenance_evidence_refs=provenance,
        review_ref=f"review://independent/{incident_id}",
        review_accepted=accepted,
        recorded_at=NOW + timedelta(minutes=2),
    )


def _release(
    *,
    disposition: ReleaseDisposition = ReleaseDisposition.HEALTHY,
    restored_release_sha: str | None = None,
    health_refs: tuple[str, ...] = ("health://candidate/green",),
) -> ReleaseEvidence:
    return ReleaseEvidence(
        release_event_id="release:incident-1",
        incident_id="incident-1",
        candidate_id="candidate:incident-1:22222222",
        previous_release_sha=OLD_SHA,
        candidate_release_sha=NEW_SHA,
        artifact_digest=ARTIFACT,
        disposition=disposition,
        deployment_evidence_refs=("deploy://staging/green", "deploy://production/attempt"),
        health_evidence_refs=health_refs,
        restored_release_sha=restored_release_sha,
        reconciliation_ref=None,
        observed_at=NOW + timedelta(minutes=3),
    )


def _planned() -> IncidentRepairReleaseCoordinator:
    coordinator = IncidentRepairReleaseCoordinator("project-a")
    coordinator.open_incident("incident-1", _trigger(), _operations().snapshot())
    coordinator.create_repair_work_order(_work_order())
    return coordinator


def test_health_incident_requires_exact_integrated_operations_evidence() -> None:
    coordinator = IncidentRepairReleaseCoordinator("project-a")
    operations = _operations().snapshot()

    opened = coordinator.open_incident("incident-1", _trigger(), operations)
    assert opened.state is IncidentState.OPEN
    assert opened.trigger.release_sha == OLD_SHA

    with pytest.raises(ProductIncidentError, match="not present"):
        coordinator.open_incident(
            "incident-forged",
            replace(_trigger(), evidence_refs=("health://forged",)),
            operations,
        )

    with pytest.raises(ProductIncidentError, match="stale"):
        coordinator.open_incident(
            "incident-stale",
            replace(_trigger(), release_sha=OTHER_SHA),
            operations,
        )


def test_same_trigger_is_idempotent_even_when_retried_with_new_incident_id() -> None:
    coordinator = IncidentRepairReleaseCoordinator("project-a")
    operations = _operations().snapshot()
    first = coordinator.open_incident("incident-1", _trigger(), operations)
    retried = coordinator.open_incident("incident-retry", _trigger(), operations)

    assert retried == first
    assert tuple(item.incident_id for item in coordinator.list_incidents()) == ("incident-1",)


def test_repair_work_order_is_bounded_and_preserves_trigger_evidence() -> None:
    coordinator = IncidentRepairReleaseCoordinator("project-a")
    coordinator.open_incident("incident-1", _trigger(), _operations().snapshot())

    with pytest.raises(ProductIncidentError, match="normalized project-relative"):
        replace(_work_order(), allowed_paths=("../outside.py",))

    with pytest.raises(ProductIncidentError, match="preserve incident evidence"):
        coordinator.create_repair_work_order(
            replace(_work_order(), evidence_refs=("incident://summary-only",))
        )

    planned = coordinator.create_repair_work_order(_work_order())
    assert planned.state is IncidentState.PLANNED
    assert planned.work_order is not None


def test_rejected_candidate_cannot_be_released_and_accepted_repair_can() -> None:
    coordinator = _planned()
    rejected = coordinator.record_candidate(_candidate(accepted=False))
    assert rejected.state is IncidentState.REVIEW_REQUIRED

    with pytest.raises(ProductIncidentError, match="accepted"):
        coordinator.record_release(_release())

    accepted_candidate = replace(
        _candidate(accepted=True, result_sha=OTHER_SHA),
        candidate_id="candidate:incident-1:33333333",
    )
    accepted = coordinator.record_candidate(accepted_candidate)
    assert accepted.state is IncidentState.RELEASE_READY

    healthy_release = replace(
        _release(),
        candidate_id=accepted_candidate.candidate_id,
        candidate_release_sha=OTHER_SHA,
    )
    resolved = coordinator.record_release(healthy_release)
    assert resolved.state is IncidentState.RESOLVED


def test_bad_candidate_can_roll_back_only_to_exact_prior_release() -> None:
    coordinator = _planned()
    coordinator.record_candidate(_candidate())

    with pytest.raises(ProductIncidentError, match="exact known-good"):
        _release(
            disposition=ReleaseDisposition.ROLLED_BACK,
            restored_release_sha=OTHER_SHA,
            health_refs=("health://candidate/bad", "rollback://verify"),
        )

    rolled_back = coordinator.record_release(
        _release(
            disposition=ReleaseDisposition.ROLLED_BACK,
            restored_release_sha=OLD_SHA,
            health_refs=("health://candidate/bad", "rollback://verify"),
        )
    )
    assert rolled_back.state is IncidentState.ROLLED_BACK
    assert rolled_back.release_events[-1].restored_release_sha == OLD_SHA


def test_uncertain_release_requires_inspection_reconciliation_not_redeploy() -> None:
    coordinator = _planned()
    coordinator.record_candidate(_candidate())
    uncertain = coordinator.record_release(
        _release(
            disposition=ReleaseDisposition.UNCERTAIN,
            restored_release_sha=None,
            health_refs=(),
        )
    )
    assert uncertain.state is IncidentState.RECONCILE_REQUIRED

    with pytest.raises(ProductIncidentError, match="reconciled"):
        coordinator.record_release(
            replace(
                _release(),
                release_event_id="release:blind-retry",
            )
        )

    resolved = coordinator.reconcile_release(
        "incident-1",
        reconciliation_ref="inspect://deployment/op-1",
        disposition=ReleaseDisposition.HEALTHY,
        health_evidence_refs=("health://candidate/green-after-inspect",),
        restored_release_sha=None,
        observed_at=NOW + timedelta(minutes=5),
    )
    assert resolved.state is IncidentState.RESOLVED
    assert len(resolved.release_events) == 1
    assert resolved.release_events[0].reconciliation_ref == "inspect://deployment/op-1"


def test_dependency_security_repair_preserves_advisory_and_fixed_version() -> None:
    advisory = SupplyChainAdvisory(
        advisory_id="GHSA-test-1234",
        ecosystem="PyPI",
        package_name="example-lib",
        affected_version="1.0.0",
        fixed_version="1.0.1",
        provenance_ref="advisory://GHSA-test-1234",
    )
    trigger = IncidentTrigger(
        project_id="project-a",
        service_id="api",
        environment_id="prod-eu",
        release_sha=OLD_SHA,
        kind=IncidentKind.DEPENDENCY,
        severity=IncidentSeverity.HIGH,
        evidence_refs=("advisory://GHSA-test-1234", "sbom://release-old"),
        approval_ref="approval://incident/security-1",
        observed_at=NOW,
        advisory=advisory,
    )
    coordinator = IncidentRepairReleaseCoordinator("project-a")
    coordinator.open_incident("incident-1", trigger, _operations().snapshot())

    with pytest.raises(ProductIncidentError, match="fixed version"):
        coordinator.create_repair_work_order(
            _work_order(
                evidence_refs=trigger.evidence_refs,
                advisory_id=advisory.advisory_id,
                fixed_version="9.9.9",
            )
        )

    coordinator.create_repair_work_order(
        _work_order(
            evidence_refs=trigger.evidence_refs,
            advisory_id=advisory.advisory_id,
            fixed_version="1.0.1",
        )
    )
    with pytest.raises(ProductIncidentError, match="preserve advisory provenance"):
        coordinator.record_candidate(_candidate(provenance=("build://exact-sha",)))

    ready = coordinator.record_candidate(
        _candidate(provenance=("build://exact-sha", advisory.provenance_ref))
    )
    assert ready.state is IncidentState.RELEASE_READY


def test_snapshot_round_trip_is_canonical_restart_safe_and_contains_no_secret_material() -> None:
    coordinator = _planned()
    coordinator.record_candidate(_candidate())
    before = coordinator.snapshot()
    payload = dump_incident_snapshot(before)

    restored_snapshot = load_incident_snapshot(payload)
    restarted = IncidentRepairReleaseCoordinator("project-a")
    restarted.restore(restored_snapshot)

    assert restarted.snapshot() == before
    assert dump_incident_snapshot(restarted.snapshot()) == payload
    assert "password" not in payload.casefold()
    assert "api_key" not in payload.casefold()
    assert "lease_token" not in payload.casefold()


def test_tampered_snapshot_relationship_and_schema_fail_closed() -> None:
    coordinator = _planned()
    coordinator.record_candidate(_candidate())
    payload = json.loads(dump_incident_snapshot(coordinator.snapshot()))

    payload["incidents"][0]["candidates"][0]["base_release_sha"] = OTHER_SHA
    with pytest.raises(ProductIncidentError, match="base release"):
        load_incident_snapshot(json.dumps(payload))

    payload = json.loads(dump_incident_snapshot(coordinator.snapshot()))
    payload["schema"] = "nika-pf3-incident-repair-release-v999"
    with pytest.raises(ProductIncidentError, match="unsupported"):
        load_incident_snapshot(json.dumps(payload))


def test_cross_project_incident_and_snapshot_are_rejected() -> None:
    coordinator = IncidentRepairReleaseCoordinator("project-a")
    with pytest.raises(ProductIncidentError, match="another project"):
        coordinator.open_incident(
            "incident-x",
            replace(_trigger(), project_id="project-b"),
            _operations(project_id="project-b").snapshot(),
        )

    source = _planned()
    foreign = IncidentRepairReleaseCoordinator("project-b")
    with pytest.raises(ProductIncidentError, match="another project"):
        foreign.restore(source.snapshot())


def test_scale_50_services_restart_without_cross_incident_aliasing() -> None:
    coordinator = IncidentRepairReleaseCoordinator("project-a")
    for index in range(50):
        service = f"svc-{index:02d}"
        operations = _operations(service_id=service).snapshot()
        incident_id = f"incident-{index:02d}"
        trigger = _trigger(service_id=service)
        coordinator.open_incident(incident_id, trigger, operations)
        coordinator.create_repair_work_order(
            _work_order(incident_id, service_id=service)
        )

    payload = dump_incident_snapshot(coordinator.snapshot())
    restarted = IncidentRepairReleaseCoordinator("project-a")
    restarted.restore(load_incident_snapshot(payload))

    incidents = restarted.list_incidents()
    assert len(incidents) == 50
    assert len({item.trigger.fingerprint for item in incidents}) == 50
    assert all(item.state is IncidentState.PLANNED for item in incidents)
