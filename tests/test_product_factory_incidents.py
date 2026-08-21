from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from nika_core.product_factory_coordinator import (
    ComponentWorkRequest,
    CoordinatorSnapshot,
    ReviewDecision,
    WorkerResultEnvelope,
    WorkRecord,
    WorkState,
)
from nika_core.product_factory_deployment import (
    DeploymentFabricSnapshot,
    DeploymentIntent,
    DeploymentRecord,
    DeploymentState,
    EnvironmentIdentity,
    EnvironmentTier,
    HealthEvidence,
    ReleaseRef,
    RollbackEvidence,
)
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
from nika_core.toolsmith.contracts import (
    ArtifactEvidence,
    ChangedFile,
    CodingResult,
    TestEvidence,
)

OLD_SHA = "1" * 40
NEW_SHA = "2" * 40
OTHER_SHA = "3" * 40
ARTIFACT = "a" * 64
OTHER_ARTIFACT = "c" * 64
DIFF = "b" * 64
FILE_DIGEST = "d" * 64
TEST_DIGEST = "e" * 64
NOW = datetime(2026, 8, 21, 10, 0, tzinfo=UTC)
PERMISSIONS = frozenset({"repo.read", "repo.write", "tests.run"})
DEPLOY_REFS = ("deploy://staging/green", "deploy://production/attempt")


def operations(service: str = "api", project: str = "project-a") -> ProductOperationsCoordinator:
    value = ProductOperationsCoordinator(project)
    value.register(
        DeployableService(
            service,
            project,
            "prod-eu",
            OLD_SHA,
            0,
            (ServiceReplica(f"{service}-r1", "node-1"), ServiceReplica(f"{service}-r2", "node-2")),
        )
    )
    value.record_observation(
        ServiceObservation(
            service,
            OLD_SHA,
            (f"{service}-r1",),
            (f"{service}-r2",),
            (f"health://{service}/degraded",),
            NOW,
        )
    )
    return value


def trigger(service: str = "api", project: str = "project-a") -> IncidentTrigger:
    return IncidentTrigger(
        project,
        service,
        "prod-eu",
        OLD_SHA,
        IncidentKind.HEALTH,
        IncidentSeverity.HIGH,
        (f"health://{service}/degraded",),
        "approval://incident/1",
        NOW,
    )


def order(
    incident: str = "incident-1",
    service: str = "api",
    *,
    evidence: tuple[str, ...] | None = None,
    advisory_id: str | None = None,
    fixed: str | None = None,
) -> RepairWorkOrder:
    return RepairWorkOrder(
        f"repair:{incident}",
        incident,
        "project-a",
        service,
        "repo-a",
        f"component-{service}",
        OLD_SHA,
        "Repair the incident without widening component ownership.",
        (f"src/{service}.py", f"tests/test_{service}.py"),
        PERMISSIONS,
        (("python", "-m", "pytest", f"tests/test_{service}.py"),),
        evidence or (f"health://{service}/degraded",),
        NOW + timedelta(minutes=1),
        advisory_id,
        fixed,
    )


def candidate(
    incident: str = "incident-1",
    *,
    accepted: bool = True,
    result_sha: str = NEW_SHA,
    provenance: tuple[str, ...] = ("build://exact-sha",),
) -> RepairCandidateEvidence:
    return RepairCandidateEvidence(
        f"candidate:{incident}:{result_sha[:8]}",
        incident,
        f"repair:{incident}",
        OLD_SHA,
        result_sha,
        ARTIFACT,
        DIFF,
        (TEST_DIGEST,),
        provenance,
        f"review://independent/{incident}",
        accepted,
        NOW + timedelta(minutes=2),
    )


def authority(
    work: RepairWorkOrder,
    item: RepairCandidateEvidence,
    *,
    permission_ceiling: frozenset[str] | None = None,
    result_sha: str | None = None,
    artifact_digest: str | None = None,
    review_refs: tuple[str, ...] | None = None,
) -> CoordinatorSnapshot:
    request = ComponentWorkRequest(
        work.work_order_id,
        work.project_id,
        work.component_id,
        work.repository_id,
        work.goal,
        work.base_release_sha,
        work.allowed_paths,
        work.permission_ceiling if permission_ceiling is None else permission_ceiling,
        work.acceptance_commands,
    )
    coding = CodingResult(
        work.work_order_id,
        changed_files=(ChangedFile(work.allowed_paths[0], FILE_DIGEST, 42),),
        test_evidence=(TestEvidence(work.acceptance_commands[0], 0, TEST_DIGEST),),
        artifacts=(
            ArtifactEvidence(
                "repair-package",
                item.artifact_digest if artifact_digest is None else artifact_digest,
                "application/zip",
            ),
        ),
    )
    result = WorkerResultEnvelope(
        work.work_order_id,
        work.component_id,
        work.repository_id,
        work.base_release_sha,
        item.result_sha if result_sha is None else result_sha,
        item.diff_digest,
        coding,
    )
    review = ReviewDecision(
        "independent-qa",
        item.review_accepted,
        "Independent repair acceptance decision.",
        (item.review_ref,) if review_refs is None else review_refs,
    )
    state = WorkState.ACCEPTED if item.review_accepted else WorkState.REPAIR_REQUIRED
    return CoordinatorSnapshot(work.project_id, 1, (WorkRecord(request, state, result, review),))


def deployments(
    disposition: ReleaseDisposition = ReleaseDisposition.HEALTHY,
    *,
    result_sha: str = NEW_SHA,
    artifact: str = ARTIFACT,
    environment: str = "prod-eu",
    health_refs: tuple[str, ...] = ("health://candidate/green",),
) -> DeploymentFabricSnapshot:
    release = ReleaseRef("project-a", "repair-1", result_sha, artifact)
    staging_env = EnvironmentIdentity(
        "staging-eu", "project-a", EnvironmentTier.STAGING, "provider://fake/staging"
    )
    production_env = EnvironmentIdentity(
        environment, "project-a", EnvironmentTier.PRODUCTION, "provider://fake/production"
    )
    staging = DeploymentRecord(
        DeploymentIntent("intent-staging", "project-a", staging_env, release),
        DeploymentState.HEALTHY,
        (DEPLOY_REFS[0],),
        HealthEvidence("staging-eu", result_sha, True, ("health://staging/green",), NOW),
    )
    production_intent = DeploymentIntent(
        "intent-production", "project-a", production_env, release
    )
    if disposition is ReleaseDisposition.HEALTHY:
        production = DeploymentRecord(
            production_intent,
            DeploymentState.HEALTHY,
            (DEPLOY_REFS[1],),
            HealthEvidence(environment, result_sha, True, health_refs, NOW),
            previous_release_sha=OLD_SHA,
        )
    elif disposition is ReleaseDisposition.ROLLED_BACK:
        production = DeploymentRecord(
            production_intent,
            DeploymentState.ROLLED_BACK,
            (DEPLOY_REFS[1],),
            HealthEvidence(
                environment,
                result_sha,
                False,
                ("health://candidate/bad",),
                NOW,
            ),
            RollbackEvidence(
                environment,
                result_sha,
                OLD_SHA,
                True,
                ("rollback://verify",),
            ),
            OLD_SHA,
        )
    else:
        production = DeploymentRecord(
            production_intent,
            DeploymentState.UNCERTAIN,
            (DEPLOY_REFS[1],),
            previous_release_sha=OLD_SHA,
        )
    return DeploymentFabricSnapshot(
        (staging, production),
        (("project-a", result_sha),),
        (),
    )


def release(
    disposition: ReleaseDisposition = ReleaseDisposition.HEALTHY,
    *,
    item: RepairCandidateEvidence | None = None,
    health_refs: tuple[str, ...] = ("health://candidate/green",),
    restored: str | None = None,
    deploy_refs: tuple[str, ...] = DEPLOY_REFS,
    observed_at: datetime | None = None,
) -> ReleaseEvidence:
    candidate_value = item or candidate()
    return ReleaseEvidence(
        "release:incident-1",
        "incident-1",
        candidate_value.candidate_id,
        OLD_SHA,
        candidate_value.result_sha,
        candidate_value.artifact_digest,
        "intent-staging",
        "intent-production",
        disposition,
        deploy_refs,
        health_refs,
        restored,
        None,
        observed_at or NOW + timedelta(minutes=3),
    )


def planned() -> tuple[IncidentRepairReleaseCoordinator, RepairWorkOrder]:
    value = IncidentRepairReleaseCoordinator("project-a")
    value.open_incident("incident-1", trigger(), operations().snapshot())
    work = order()
    value.create_repair_work_order(work)
    return value, work


def reviewed(
    *, accepted: bool = True, result_sha: str = NEW_SHA
) -> tuple[
    IncidentRepairReleaseCoordinator,
    RepairWorkOrder,
    RepairCandidateEvidence,
    CoordinatorSnapshot,
]:
    value, work = planned()
    item = candidate(accepted=accepted, result_sha=result_sha)
    proof = authority(work, item)
    value.record_candidate(item, proof)
    return value, work, item, proof


def test_incident_trigger_is_operations_bound_and_idempotent() -> None:
    value = IncidentRepairReleaseCoordinator("project-a")
    snapshot = operations().snapshot()
    first = value.open_incident("incident-1", trigger(), snapshot)
    assert value.open_incident("incident-retry", trigger(), snapshot) == first
    with pytest.raises(ProductIncidentError, match="not present"):
        value.open_incident(
            "forged",
            replace(trigger(), evidence_refs=("health://forged",)),
            snapshot,
        )
    with pytest.raises(ProductIncidentError, match="stale"):
        value.open_incident("stale", replace(trigger(), release_sha=OTHER_SHA), snapshot)


def test_work_order_preserves_scope_permissions_evidence_and_time() -> None:
    value = IncidentRepairReleaseCoordinator("project-a")
    value.open_incident("incident-1", trigger(), operations().snapshot())
    with pytest.raises(ProductIncidentError, match="normalized project-relative"):
        replace(order(), allowed_paths=("../outside.py",))
    with pytest.raises(ProductIncidentError, match="permission ceiling"):
        replace(order(), permission_ceiling=frozenset())
    with pytest.raises(ProductIncidentError, match="preserve incident evidence"):
        value.create_repair_work_order(replace(order(), evidence_refs=("summary://only",)))
    with pytest.raises(ProductIncidentError, match="predate"):
        value.create_repair_work_order(replace(order(), created_at=NOW - timedelta(seconds=1)))
    assert value.create_repair_work_order(order()).state is IncidentState.PLANNED


def test_candidate_requires_exact_worker_test_artifact_review_and_permission_authority() -> None:
    value, work = planned()
    item = candidate()
    assert value.record_candidate(item, authority(work, item)).state is IncidentState.RELEASE_READY

    mutations = (
        (authority(work, item, permission_ceiling=frozenset({"repo.read"})), "permission ceiling"),
        (authority(work, item, result_sha=OTHER_SHA), "exact worker evidence"),
        (authority(work, item, artifact_digest=OTHER_ARTIFACT), "artifact"),
        (authority(work, item, review_refs=("review://forged",)), "review ref"),
    )
    for proof, message in mutations:
        other, _ = planned()
        with pytest.raises(ProductIncidentError, match=message):
            other.record_candidate(item, proof)


def test_rejected_candidate_cannot_release_but_later_accepted_candidate_can() -> None:
    value, work = planned()
    rejected = candidate(accepted=False)
    value.record_candidate(rejected, authority(work, rejected))
    with pytest.raises(ProductIncidentError, match="accepted"):
        value.record_release(release(item=rejected), deployments())

    accepted = replace(
        candidate(result_sha=OTHER_SHA),
        candidate_id="candidate:incident-1:33333333",
    )
    value.record_candidate(accepted, authority(work, accepted))
    result = value.record_release(
        release(item=accepted),
        deployments(result_sha=OTHER_SHA),
    )
    assert result.state is IncidentState.RESOLVED


def test_release_is_exact_staging_production_health_evidence_bound() -> None:
    value, _, item, _ = reviewed()
    assert value.record_release(release(item=item), deployments()).state is IncidentState.RESOLVED

    value, _, item, _ = reviewed()
    with pytest.raises(ProductIncidentError, match="deployment refs"):
        value.record_release(release(item=item, deploy_refs=("deploy://forged",)), deployments())

    value, _, item, _ = reviewed()
    with pytest.raises(ProductIncidentError, match="health refs"):
        value.record_release(release(item=item, health_refs=("health://forged",)), deployments())

    value, _, item, _ = reviewed()
    with pytest.raises(ProductIncidentError, match="production deployment authority"):
        value.record_release(release(item=item), deployments(environment="prod-wrong"))

    value, _, item, _ = reviewed()
    with pytest.raises(ProductIncidentError, match="staging deployment authority"):
        value.record_release(release(item=item), deployments(artifact=OTHER_ARTIFACT))


def test_rollback_is_bound_to_exact_prior_release_and_authoritative_rollback() -> None:
    value, _, item, _ = reviewed()
    with pytest.raises(ProductIncidentError, match="exact known-good"):
        release(
            ReleaseDisposition.ROLLED_BACK,
            item=item,
            health_refs=("health://candidate/bad", "rollback://verify"),
            restored=OTHER_SHA,
        )
    result = value.record_release(
        release(
            ReleaseDisposition.ROLLED_BACK,
            item=item,
            health_refs=("health://candidate/bad", "rollback://verify"),
            restored=OLD_SHA,
        ),
        deployments(ReleaseDisposition.ROLLED_BACK),
    )
    assert result.state is IncidentState.ROLLED_BACK


def test_uncertain_release_can_only_reconcile_through_inspection_evidence() -> None:
    value, _, item, _ = reviewed()
    uncertain = release(
        ReleaseDisposition.UNCERTAIN,
        item=item,
        health_refs=(),
    )
    value.record_release(uncertain, deployments(ReleaseDisposition.UNCERTAIN))
    with pytest.raises(ProductIncidentError, match="reconciled"):
        value.record_release(replace(release(item=item), release_event_id="blind"), deployments())
    result = value.reconcile_release(
        "incident-1",
        reconciliation_ref="inspect://deployment/op-1",
        disposition=ReleaseDisposition.HEALTHY,
        health_evidence_refs=("inspect://deployment/op-1",),
        restored_release_sha=None,
        observed_at=NOW + timedelta(minutes=5),
        deployments=deployments(health_refs=("inspect://deployment/op-1",)),
    )
    assert result.state is IncidentState.RESOLVED
    assert len(result.release_events) == 1


def test_supply_chain_incident_preserves_advisory_fixed_version_and_provenance() -> None:
    advisory = SupplyChainAdvisory(
        "GHSA-test-1234",
        "PyPI",
        "example-lib",
        "1.0.0",
        "1.0.1",
        "advisory://GHSA-test-1234",
    )
    event = IncidentTrigger(
        "project-a",
        "api",
        "prod-eu",
        OLD_SHA,
        IncidentKind.DEPENDENCY,
        IncidentSeverity.HIGH,
        (advisory.provenance_ref, "sbom://release-old"),
        "approval://incident/security-1",
        NOW,
        advisory,
    )
    value = IncidentRepairReleaseCoordinator("project-a")
    value.open_incident("incident-1", event, operations().snapshot())
    with pytest.raises(ProductIncidentError, match="fixed version"):
        value.create_repair_work_order(
            order(evidence=event.evidence_refs, advisory_id=advisory.advisory_id, fixed="9.9.9")
        )
    work = order(
        evidence=event.evidence_refs,
        advisory_id=advisory.advisory_id,
        fixed="1.0.1",
    )
    value.create_repair_work_order(work)
    bad = candidate()
    with pytest.raises(ProductIncidentError, match="advisory provenance"):
        value.record_candidate(bad, authority(work, bad))
    good = candidate(provenance=("build://exact-sha", advisory.provenance_ref))
    assert value.record_candidate(good, authority(work, good)).state is IncidentState.RELEASE_READY


def test_timeline_is_monotonic_through_candidate_release_and_reconcile() -> None:
    value, work = planned()
    early = replace(candidate(), recorded_at=work.created_at - timedelta(seconds=1))
    with pytest.raises(ProductIncidentError, match="predate"):
        value.record_candidate(early, authority(work, early))

    value, _, item, _ = reviewed()
    with pytest.raises(ProductIncidentError, match="predate"):
        value.record_release(
            release(item=item, observed_at=item.recorded_at - timedelta(seconds=1)),
            deployments(),
        )

    value, _, item, _ = reviewed()
    uncertain = release(ReleaseDisposition.UNCERTAIN, item=item, health_refs=())
    value.record_release(uncertain, deployments(ReleaseDisposition.UNCERTAIN))
    with pytest.raises(ProductIncidentError, match="predate"):
        value.reconcile_release(
            "incident-1",
            reconciliation_ref="inspect://deployment/op-1",
            disposition=ReleaseDisposition.HEALTHY,
            health_evidence_refs=("inspect://deployment/op-1",),
            restored_release_sha=None,
            observed_at=uncertain.observed_at - timedelta(seconds=1),
            deployments=deployments(health_refs=("inspect://deployment/op-1",)),
        )


def test_restart_requires_external_authority_for_candidate_and_release_state() -> None:
    value, _, item, review = reviewed()
    candidate_payload = dump_incident_snapshot(value.snapshot())
    with pytest.raises(ProductIncidentError, match="review authority"):
        load_incident_snapshot(candidate_payload)
    assert load_incident_snapshot(
        candidate_payload,
        review_authorities=(review,),
    ).incidents[0].candidates[0] == item

    deploy = deployments()
    value.record_release(release(item=item), deploy)
    payload = dump_incident_snapshot(value.snapshot())
    with pytest.raises(ProductIncidentError, match="review authority"):
        load_incident_snapshot(payload, deployments=deploy)
    with pytest.raises(ProductIncidentError, match="deployment authority"):
        load_incident_snapshot(payload, review_authorities=(review,))
    restored = load_incident_snapshot(
        payload,
        deployments=deploy,
        review_authorities=(review,),
    )
    assert restored.incidents[0].state is IncidentState.RESOLVED


def test_snapshot_tamper_schema_timeline_and_authority_evidence_fail_closed() -> None:
    value, _ = planned()
    original = dump_incident_snapshot(value.snapshot())
    payload = json.loads(original)
    payload["incidents"][0]["work_order"]["base_release_sha"] = OTHER_SHA
    with pytest.raises(ProductIncidentError, match="base release"):
        load_incident_snapshot(json.dumps(payload))

    payload = json.loads(original)
    payload["schema"] = "nika-pf3-incident-repair-release-v999"
    with pytest.raises(ProductIncidentError, match="unsupported"):
        load_incident_snapshot(json.dumps(payload))

    payload = json.loads(original)
    payload["incidents"][0]["work_order"]["created_at"] = (
        NOW - timedelta(minutes=1)
    ).isoformat()
    with pytest.raises(ProductIncidentError, match="predates"):
        load_incident_snapshot(json.dumps(payload))

    value, _, item, review = reviewed()
    deploy = deployments()
    value.record_release(release(item=item), deploy)
    payload = json.loads(dump_incident_snapshot(value.snapshot()))
    payload["incidents"][0]["release_events"][0]["health_evidence_refs"] = ["forged"]
    with pytest.raises(ProductIncidentError, match="health refs"):
        load_incident_snapshot(
            json.dumps(payload),
            deployments=deploy,
            review_authorities=(review,),
        )


def test_planned_snapshot_is_canonical_secret_free_and_cross_project_safe() -> None:
    value, _ = planned()
    payload = dump_incident_snapshot(value.snapshot())
    restored = load_incident_snapshot(payload)
    restarted = IncidentRepairReleaseCoordinator("project-a")
    restarted.restore(restored)
    assert dump_incident_snapshot(restarted.snapshot()) == payload
    forbidden = ("password", "api_key", "lease_token")
    assert all(marker not in payload.casefold() for marker in forbidden)

    foreign = IncidentRepairReleaseCoordinator("project-b")
    with pytest.raises(ProductIncidentError, match="another project"):
        foreign.restore(restored)


def test_scale_50_services_restart_without_cross_incident_aliasing() -> None:
    value = IncidentRepairReleaseCoordinator("project-a")
    for index in range(50):
        service = f"svc-{index:02d}"
        incident = f"incident-{index:02d}"
        value.open_incident(incident, trigger(service), operations(service).snapshot())
        value.create_repair_work_order(order(incident, service))
    payload = dump_incident_snapshot(value.snapshot())
    restarted = IncidentRepairReleaseCoordinator("project-a")
    restarted.restore(load_incident_snapshot(payload))
    records = restarted.list_incidents()
    assert len(records) == 50
    assert len({record.trigger.fingerprint for record in records}) == 50
    assert all(record.state is IncidentState.PLANNED for record in records)
