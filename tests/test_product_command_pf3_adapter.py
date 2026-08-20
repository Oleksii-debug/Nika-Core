from __future__ import annotations

from datetime import UTC, datetime

from nika_core.product_command.contracts import ProductStatusKind
from nika_core.product_command.deployment_adapter import (
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
NOW = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)


def _intent() -> DeploymentIntent:
    return DeploymentIntent(
        "stage-1",
        "project-1",
        EnvironmentIdentity(
            "env-stage",
            "project-1",
            EnvironmentTier.STAGING,
            "credential://provider/SHOULD-NOT-LEAK",
        ),
        ReleaseRef("project-1", "1.0.0", SHA_A, DIGEST_A),
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
    intent = _intent()
    health = HealthEvidence("env-stage", SHA_A, True, ("health://ok",), NOW)
    record = DeploymentRecord(
        intent,
        DeploymentState.HEALTHY,
        ("deploy://ok",),
        health=health,
    )
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
    assert "credential://provider/SHOULD-NOT-LEAK" not in serialized


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
    snapshot = DeploymentFabricSnapshot((record,), (), (("env-stage", SHA_B),))

    entries = deployment_status_entries(snapshot)

    deployment = next(item for item in entries if item.kind is ProductStatusKind.DEPLOYMENT)
    assert deployment.state == "rolled_back"
    assert "Health: unhealthy" in deployment.detail
    assert "Rollback: succeeded" in deployment.detail
    assert f"Restored release SHA: {SHA_B}" in deployment.detail
    assert {item.kind for item in deployment.evidence} == {
        "deployment",
        "health",
        "rollback",
    }
