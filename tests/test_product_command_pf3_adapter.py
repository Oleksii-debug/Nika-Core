from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nika_core.product_command.contracts import ProductStatusKind
from nika_core.product_command.deployment_adapter import (
    DeploymentPresentationIntegrityError,
    deployment_status_entries,
    execution_status_entries,
)
from nika_core.product_factory_deployment import (
    DeploymentFabricSnapshot,
    DeploymentIntent,
    DeploymentRecord,
    DeploymentState,
    EnvironmentIdentity,
    EnvironmentTier,
    ExecutionNodeRegistry,
    ExecutionRequest,
    HealthEvidence,
    Platform,
    ReleaseRef,
    ResourceEnvelope,
    RollbackEvidence,
    local_windows_node,
)

SHA_A = "a" * 40
SHA_B = "b" * 40
DIGEST_A = "1" * 64
DIGEST_B = "2" * 64
NOW = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)


def _intent(
    *,
    project_id: str = "project-1",
    intent_id: str = "stage-1",
    sha: str = SHA_A,
    digest: str = DIGEST_A,
) -> DeploymentIntent:
    return DeploymentIntent(
        intent_id,
        project_id,
        EnvironmentIdentity(
            "env-stage",
            project_id,
            EnvironmentTier.STAGING,
            f"credential://provider/{project_id}/SHOULD-NOT-LEAK",
        ),
        ReleaseRef(project_id, "1.0.0", sha, digest),
    )


def _healthy_record(
    *,
    project_id: str = "project-1",
    intent_id: str = "stage-healthy",
    sha: str = SHA_A,
    digest: str = DIGEST_A,
) -> DeploymentRecord:
    return DeploymentRecord(
        _intent(
            project_id=project_id,
            intent_id=intent_id,
            sha=sha,
            digest=digest,
        ),
        DeploymentState.HEALTHY,
        (f"deploy://{project_id}/{intent_id}",),
        health=HealthEvidence(
            "env-stage",
            sha,
            True,
            (f"health://{project_id}/{intent_id}",),
            NOW,
        ),
    )


def test_execution_nodes_and_leases_are_textually_inspectable() -> None:
    registry = ExecutionNodeRegistry()
    registry.register(local_windows_node())
    registry.acquire(
        ExecutionRequest(
            "project-1",
            "work-win",
            Platform.WINDOWS,
            frozenset({"package"}),
            frozenset({"python"}),
            ResourceEnvelope(1, 512, 512),
        ),
        now=NOW,
    )

    entries = execution_status_entries(registry.snapshot())

    node = next(item for item in entries if item.item_id == "execution-node:local-windows")
    lease = next(item for item in entries if item.item_id.startswith("execution-lease:"))
    assert node.kind is ProductStatusKind.BUILD
    assert node.state == "enabled"
    assert "Platform: windows" in node.detail
    assert "active leases: 1" in node.detail
    assert lease.owner == "local-windows"
    assert "work: work-win" in lease.detail


def test_healthy_staging_exposes_release_health_and_exact_evidence_without_provider_ref() -> None:
    record = _healthy_record(intent_id="stage-1")
    snapshot = DeploymentFabricSnapshot(
        (record,),
        (("project-1", SHA_A),),
        (("env-stage", SHA_A),),
    )

    entries = deployment_status_entries(snapshot)
    serialized = "\n".join(item.model_dump_json() for item in entries)

    release = next(item for item in entries if item.kind is ProductStatusKind.RELEASE)
    deployment = next(item for item in entries if item.kind is ProductStatusKind.DEPLOYMENT)
    assert release.state == "candidate"
    assert SHA_A in release.detail
    assert {item.kind for item in release.evidence} == {"git_commit", "artifact_digest"}
    assert deployment.state == "healthy"
    assert "Health: healthy" in deployment.detail
    assert {item.kind for item in deployment.evidence} == {"deployment", "health"}
    assert "credential://provider/project-1/SHOULD-NOT-LEAK" not in serialized


def test_uncertain_deployment_creates_explicit_blocker() -> None:
    record = DeploymentRecord(
        _intent(),
        DeploymentState.UNCERTAIN,
        ("deploy://timeout",),
    )
    snapshot = DeploymentFabricSnapshot((record,), (), ())

    entries = deployment_status_entries(snapshot)

    blocker = next(item for item in entries if item.kind is ProductStatusKind.BLOCKER)
    assert blocker.state == "active"
    assert "requires reconciliation" in blocker.detail
    assert blocker.evidence[0].reference == "deploy://timeout"


def test_rollback_state_preserves_failure_and_restore_evidence() -> None:
    previous = _healthy_record(
        intent_id="stage-previous",
        sha=SHA_B,
        digest=DIGEST_B,
    )
    intent = _intent()
    health = HealthEvidence("env-stage", SHA_A, False, ("health://bad",), NOW)
    rollback = RollbackEvidence(
        "env-stage",
        SHA_A,
        SHA_B,
        True,
        ("rollback://ok",),
    )
    record = DeploymentRecord(
        intent,
        DeploymentState.ROLLED_BACK,
        ("deploy://ok",),
        health=health,
        rollback=rollback,
        previous_release_sha=SHA_B,
    )
    snapshot = DeploymentFabricSnapshot(
        (previous, record),
        (("project-1", SHA_B),),
        (("project-1", "env-stage", SHA_B),),
    )

    entries = deployment_status_entries(snapshot)

    deployment = next(
        item for item in entries if item.item_id == "deployment:stage-1"
    )
    assert deployment.state == "rolled_back"
    assert "Health: unhealthy" in deployment.detail
    assert "Rollback: succeeded" in deployment.detail
    assert f"Restored release SHA: {SHA_B}" in deployment.detail
    assert {item.kind for item in deployment.evidence} == {
        "deployment",
        "health",
        "rollback",
    }


def test_direct_adapter_rejects_rollback_only_current_release_marker() -> None:
    intent = _intent()
    record = DeploymentRecord(
        intent,
        DeploymentState.ROLLED_BACK,
        ("deploy://ok",),
        health=HealthEvidence("env-stage", SHA_A, False, ("health://bad",), NOW),
        rollback=RollbackEvidence(
            "env-stage",
            SHA_A,
            SHA_B,
            True,
            ("rollback://ok",),
        ),
        previous_release_sha=SHA_B,
    )
    snapshot = DeploymentFabricSnapshot(
        (record,),
        (),
        (("project-1", "env-stage", SHA_B),),
    )

    with pytest.raises(
        DeploymentPresentationIntegrityError,
        match="not backed by a healthy deployment record",
    ):
        deployment_status_entries(snapshot)


def test_direct_adapter_rejects_health_check_as_durable_snapshot() -> None:
    snapshot = DeploymentFabricSnapshot(
        (
            DeploymentRecord(
                _intent(),
                DeploymentState.HEALTH_CHECK,
                ("deploy://started",),
            ),
        ),
        (),
        (),
    )

    with pytest.raises(
        DeploymentPresentationIntegrityError,
        match="must not be serialized as durable",
    ):
        deployment_status_entries(snapshot)


def test_direct_adapter_rejects_record_without_provider_evidence() -> None:
    intent = _intent()
    snapshot = DeploymentFabricSnapshot(
        (
            DeploymentRecord(
                intent,
                DeploymentState.HEALTHY,
                (),
                health=HealthEvidence(
                    "env-stage",
                    SHA_A,
                    True,
                    ("health://ok",),
                    NOW,
                ),
            ),
        ),
        (),
        (),
    )

    with pytest.raises(
        DeploymentPresentationIntegrityError,
        match="requires provider evidence",
    ):
        deployment_status_entries(snapshot)


def test_direct_adapter_legacy_release_uses_exact_healthy_sha_disambiguation() -> None:
    first = _healthy_record(
        project_id="project-1",
        intent_id="stage-p1",
        sha=SHA_A,
        digest=DIGEST_A,
    )
    second = _healthy_record(
        project_id="project-2",
        intent_id="stage-p2",
        sha=SHA_B,
        digest=DIGEST_B,
    )
    snapshot = DeploymentFabricSnapshot(
        (first, second),
        (),
        (("env-stage", SHA_A),),
    )

    deployment_status_entries(snapshot)


def test_direct_adapter_rejects_true_legacy_exact_sha_project_ambiguity() -> None:
    first = _healthy_record(
        project_id="project-1",
        intent_id="stage-p1",
        sha=SHA_A,
        digest=DIGEST_A,
    )
    second = _healthy_record(
        project_id="project-2",
        intent_id="stage-p2",
        sha=SHA_A,
        digest=DIGEST_B,
    )
    snapshot = DeploymentFabricSnapshot(
        (first, second),
        (),
        (("env-stage", SHA_A),),
    )

    with pytest.raises(
        DeploymentPresentationIntegrityError,
        match="ambiguous or not backed by one healthy project",
    ):
        deployment_status_entries(snapshot)
