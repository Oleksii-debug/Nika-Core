from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nika_core.product_command.command_center import (
    ProductCommandCenterScopeError,
    _validate_deployment_snapshot,
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
)

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)
SHA_A = "a" * 40
SHA_B = "b" * 40
DIGEST = "1" * 64


def _healthy_record(
    project_id: str,
    environment_id: str,
    source_sha: str,
    *,
    tier: EnvironmentTier = EnvironmentTier.STAGING,
) -> DeploymentRecord:
    intent = DeploymentIntent(
        f"intent:{project_id}:{environment_id}:{source_sha[:8]}",
        project_id,
        EnvironmentIdentity(
            environment_id,
            project_id,
            tier,
            f"provider://{project_id}/{environment_id}",
        ),
        ReleaseRef(project_id, "1.0.0", source_sha, DIGEST),
    )
    return DeploymentRecord(
        intent,
        DeploymentState.HEALTHY,
        (f"deploy://{project_id}/{environment_id}",),
        health=HealthEvidence(
            environment_id,
            source_sha,
            True,
            (f"health://{project_id}/{environment_id}",),
            NOW,
        ),
    )


def test_healthy_staging_marker_requires_exact_healthy_record() -> None:
    record = _healthy_record("project-1", "stage", SHA_A)
    forged = DeploymentFabricSnapshot(
        (record,),
        (("project-1", SHA_B),),
        (),
    )

    with pytest.raises(ProductCommandCenterScopeError, match="healthy-staging snapshot is not backed"):
        _validate_deployment_snapshot(forged)


def test_current_release_marker_requires_exact_healthy_record() -> None:
    record = _healthy_record("project-1", "stage", SHA_A)
    stale = DeploymentFabricSnapshot(
        (record,),
        (("project-1", SHA_A),),
        (("project-1", "stage", SHA_B),),
    )

    with pytest.raises(ProductCommandCenterScopeError, match="current-release snapshot is not backed"):
        _validate_deployment_snapshot(stale)


def test_legacy_environment_marker_uses_exact_healthy_sha_to_disambiguate_projects() -> None:
    first = _healthy_record("project-1", "shared-stage", SHA_A)
    second = _healthy_record("project-2", "shared-stage", SHA_B)
    snapshot = DeploymentFabricSnapshot(
        (first, second),
        (("project-1", SHA_A), ("project-2", SHA_B)),
        (("shared-stage", SHA_A),),
    )

    normalized = _validate_deployment_snapshot(snapshot)

    assert normalized == (("project-1", "shared-stage", SHA_A),)


def test_legacy_environment_marker_without_matching_healthy_sha_fails_closed() -> None:
    first = _healthy_record("project-1", "shared-stage", SHA_A)
    second = _healthy_record("project-2", "shared-stage", SHA_B)
    snapshot = DeploymentFabricSnapshot(
        (first, second),
        (("project-1", SHA_A), ("project-2", SHA_B)),
        (("shared-stage", "c" * 40),),
    )

    with pytest.raises(
        ProductCommandCenterScopeError,
        match="legacy current-release snapshot is ambiguous or not backed",
    ):
        _validate_deployment_snapshot(snapshot)
