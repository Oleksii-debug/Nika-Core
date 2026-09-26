from __future__ import annotations

from datetime import UTC, datetime

from nika_core.product_factory_deployment import (
    DeploymentFabric,
    DeploymentIntent,
    DeploymentState,
    EnvironmentIdentity,
    EnvironmentTier,
    HealthEvidence,
    ProviderDeploymentResult,
    ReleaseRef,
)

SHA = "a" * 40
DIGEST = "1" * 64
NOW = datetime(2026, 9, 9, 1, 0, tzinfo=UTC)


class _ShaOnlyHealthProvider:
    def deploy(self, intent: DeploymentIntent) -> ProviderDeploymentResult:
        return ProviderDeploymentResult(True, False, ("deploy://ok",))

    def health(self, intent: DeploymentIntent) -> HealthEvidence:
        return HealthEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            True,
            ("health://sha-only",),
            NOW,
        )

    def rollback(self, intent: DeploymentIntent, previous_release_sha: str | None):
        raise AssertionError("healthy path must not roll back")

    def inspect(self, intent: DeploymentIntent):
        raise AssertionError("certain deploy path must not inspect")


def test_sha_only_health_evidence_cannot_authorize_exact_release() -> None:
    release = ReleaseRef("project-1", "1.0.0", SHA, DIGEST)
    environment = EnvironmentIdentity(
        "env-staging",
        "project-1",
        EnvironmentTier.STAGING,
        "provider://fake",
    )
    intent = DeploymentIntent("intent-health-exact", "project-1", environment, release)

    record = DeploymentFabric(_ShaOnlyHealthProvider()).deploy(intent)

    assert record.state is DeploymentState.UNCERTAIN
