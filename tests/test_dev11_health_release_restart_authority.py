from datetime import UTC, datetime

import pytest

from nika_core.product_factory_deployment import (
    DeploymentFabric,
    DeploymentFabricError,
    DeploymentFabricSnapshot,
    DeploymentIntent,
    DeploymentRecord,
    DeploymentState,
    EnvironmentIdentity,
    EnvironmentTier,
    HealthEvidence,
    ReleaseRef,
)


SHA = "a" * 40
DIGEST = "b" * 64


class _NoopProvider:
    def deploy(self, intent):  # pragma: no cover
        raise AssertionError("provider must not be called during restore")

    def health(self, intent):  # pragma: no cover
        raise AssertionError("provider must not be called during restore")

    def rollback(self, intent, previous_release_sha):  # pragma: no cover
        raise AssertionError("provider must not be called during restore")

    def inspect(self, intent):  # pragma: no cover
        raise AssertionError("provider must not be called during restore")


def test_restart_rejects_healthy_snapshot_without_exact_health_release() -> None:
    release = ReleaseRef("project-1", "1.0.0", SHA, DIGEST)
    environment = EnvironmentIdentity(
        "env-staging",
        "project-1",
        EnvironmentTier.STAGING,
        "provider://fake",
    )
    intent = DeploymentIntent("intent-health-restart", "project-1", environment, release)
    health = HealthEvidence(
        environment.environment_id,
        release.source_sha,
        True,
        ("health:sha-only",),
        datetime(2026, 9, 1, tzinfo=UTC),
        release=None,
    )
    record = DeploymentRecord(
        intent,
        DeploymentState.HEALTHY,
        ("deploy:1",),
        health=health,
    )
    snapshot = DeploymentFabricSnapshot(
        records=(record,),
        healthy_staging=((intent.project_id, release.source_sha),),
        current_releases=((intent.project_id, environment.environment_id, release.source_sha),),
        exact_healthy_staging=(
            (intent.project_id, release.version, release.source_sha, release.artifact_digest),
        ),
        exact_current_releases=(
            (
                intent.project_id,
                environment.environment_id,
                release.version,
                release.source_sha,
                release.artifact_digest,
            ),
        ),
    )

    with pytest.raises(
        DeploymentFabricError,
        match="snapshot health evidence requires exact release identity",
    ):
        DeploymentFabric(_NoopProvider()).restore(snapshot)
