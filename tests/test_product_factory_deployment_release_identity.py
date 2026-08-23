from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime

import pytest

from nika_core.product_factory_deployment import (
    DeploymentFabric,
    DeploymentFabricError,
    DeploymentFabricSnapshot,
    DeploymentIntent,
    DeploymentProviderPort,
    EnvironmentIdentity,
    EnvironmentTier,
    HealthEvidence,
    ProviderDeploymentResult,
    ProviderInspection,
    ReleaseRef,
    RollbackEvidence,
)

SHA_A = "a" * 40
DIGEST_A = "1" * 64
DIGEST_B = "2" * 64
NOW = datetime(2026, 8, 23, 14, 0, tzinfo=UTC)


@dataclass
class _Provider(DeploymentProviderPort):
    deploy_calls: int = 0

    def deploy(self, intent: DeploymentIntent) -> ProviderDeploymentResult:
        self.deploy_calls += 1
        return ProviderDeploymentResult(True, False, ("deploy://ok",))

    def health(self, intent: DeploymentIntent) -> HealthEvidence:
        return HealthEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            True,
            ("health://ok",),
            NOW,
        )

    def rollback(
        self,
        intent: DeploymentIntent,
        previous_release_sha: str | None,
    ) -> RollbackEvidence:
        return RollbackEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            previous_release_sha,
            True,
            ("rollback://ok",),
        )

    def inspect(self, intent: DeploymentIntent) -> ProviderInspection:
        return ProviderInspection(intent.release.source_sha, True, ("inspect://ok",))


def _intent(
    tier: EnvironmentTier,
    intent_id: str,
    *,
    version: str = "1.0.0",
    digest: str = DIGEST_A,
) -> DeploymentIntent:
    release = ReleaseRef("project-1", version, SHA_A, digest)
    environment = EnvironmentIdentity(
        f"env-{tier.value}",
        "project-1",
        tier,
        "provider://fake",
    )
    return DeploymentIntent(intent_id, "project-1", environment, release)


def test_production_rejects_same_sha_with_different_artifact_digest() -> None:
    provider = _Provider()
    fabric = DeploymentFabric(provider)
    fabric.deploy(_intent(EnvironmentTier.STAGING, "stage"))

    with pytest.raises(DeploymentFabricError, match="exact release"):
        fabric.deploy(
            _intent(
                EnvironmentTier.PRODUCTION,
                "prod-different-artifact",
                digest=DIGEST_B,
            )
        )

    assert provider.deploy_calls == 1


def test_production_rejects_same_sha_with_different_release_version() -> None:
    provider = _Provider()
    fabric = DeploymentFabric(provider)
    fabric.deploy(_intent(EnvironmentTier.STAGING, "stage"))

    with pytest.raises(DeploymentFabricError, match="exact release"):
        fabric.deploy(
            _intent(
                EnvironmentTier.PRODUCTION,
                "prod-different-version",
                version="1.0.1",
            )
        )

    assert provider.deploy_calls == 1


def test_snapshot_persists_exact_staging_release_identity() -> None:
    provider = _Provider()
    fabric = DeploymentFabric(provider)
    staging = _intent(EnvironmentTier.STAGING, "stage")
    fabric.deploy(staging)

    snapshot = fabric.snapshot()
    assert snapshot.healthy_staging == (
        ("project-1", "1.0.0", SHA_A, DIGEST_A),
    )

    restarted = DeploymentFabric(provider)
    restarted.restore(snapshot)
    restarted.deploy(_intent(EnvironmentTier.PRODUCTION, "prod-exact"))

    with pytest.raises(DeploymentFabricError, match="exact release"):
        restarted.deploy(
            _intent(
                EnvironmentTier.PRODUCTION,
                "prod-wrong-digest",
                digest=DIGEST_B,
            )
        )


def test_legacy_staging_snapshot_migrates_when_release_is_unambiguous() -> None:
    provider = _Provider()
    fabric = DeploymentFabric(provider)
    fabric.deploy(_intent(EnvironmentTier.STAGING, "stage"))
    snapshot = fabric.snapshot()
    legacy = replace(snapshot, healthy_staging=(("project-1", SHA_A),))

    restarted = DeploymentFabric(provider)
    restarted.restore(legacy)
    rewritten = restarted.snapshot()

    assert rewritten.healthy_staging == (
        ("project-1", "1.0.0", SHA_A, DIGEST_A),
    )
    restarted.deploy(_intent(EnvironmentTier.PRODUCTION, "prod"))


def test_legacy_staging_snapshot_fails_closed_when_release_is_ambiguous() -> None:
    provider = _Provider()
    fabric = DeploymentFabric(provider)
    fabric.deploy(_intent(EnvironmentTier.STAGING, "stage-v1"))
    fabric.deploy(
        _intent(
            EnvironmentTier.STAGING,
            "stage-v2",
            version="2.0.0",
            digest=DIGEST_B,
        )
    )
    snapshot = fabric.snapshot()
    ambiguous = replace(snapshot, healthy_staging=(("project-1", SHA_A),))

    restarted = DeploymentFabric(provider)
    with pytest.raises(DeploymentFabricError, match="legacy staging snapshot is ambiguous"):
        restarted.restore(ambiguous)


def test_exact_staging_snapshot_requires_backing_healthy_record() -> None:
    provider = _Provider()
    fabric = DeploymentFabric(provider)
    fabric.deploy(_intent(EnvironmentTier.STAGING, "stage"))
    snapshot = fabric.snapshot()
    corrupted = DeploymentFabricSnapshot(
        snapshot.records,
        (("project-1", "1.0.0", SHA_A, DIGEST_B),),
        snapshot.current_releases,
    )

    restarted = DeploymentFabric(provider)
    with pytest.raises(DeploymentFabricError, match="not backed by a healthy staging record"):
        restarted.restore(corrupted)
