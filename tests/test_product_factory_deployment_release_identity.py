from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime

import pytest

from nika_core.product_factory_deployment import (
    DeploymentFabric,
    DeploymentFabricError,
    DeploymentFabricSnapshot,
    DeploymentIntent,
    DeploymentProviderPort,
    DeploymentState,
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
    health_calls: int = 0
    rollback_calls: int = 0
    deploy_uncertain: bool = False
    deploy_error: bool = False
    deploy_without_evidence: bool = False
    health_error: bool = False
    healthy: bool = True
    rollback_error: bool = False
    rollback_succeeds: bool = True
    inspection: ProviderInspection | None = None
    observe_before_deploy_result: Callable[[], None] | None = None

    def deploy(self, intent: DeploymentIntent) -> ProviderDeploymentResult:
        self.deploy_calls += 1
        if self.observe_before_deploy_result is not None:
            self.observe_before_deploy_result()
        if self.deploy_error:
            raise RuntimeError("deploy transport failed after possible mutation")
        if self.deploy_without_evidence:
            return ProviderDeploymentResult(True, False, (), release=intent.release)
        if self.deploy_uncertain:
            return ProviderDeploymentResult(
                False,
                True,
                ("deploy://uncertain",),
            )
        return ProviderDeploymentResult(
            True,
            False,
            ("deploy://ok",),
            release=intent.release,
        )

    def health(self, intent: DeploymentIntent) -> HealthEvidence:
        self.health_calls += 1
        if self.health_error:
            raise RuntimeError("health transport unavailable")
        return HealthEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            self.healthy,
            ("health://ok",),
            NOW,
            release=intent.release,
        )

    def rollback(
        self,
        intent: DeploymentIntent,
        previous_release: ReleaseRef | None,
    ) -> RollbackEvidence:
        self.rollback_calls += 1
        if self.rollback_error:
            raise RuntimeError("rollback transport unavailable")
        restored = previous_release if self.rollback_succeeds else None
        return RollbackEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            restored.source_sha if restored is not None else None,
            self.rollback_succeeds,
            ("rollback://result",),
            failed_release=intent.release,
            restored_release=restored,
        )

    def inspect(self, intent: DeploymentIntent) -> ProviderInspection:
        if self.inspection is not None:
            return self.inspection
        return ProviderInspection(
            intent.release.source_sha,
            True,
            ("inspect://ok",),
            release=intent.release,
        )


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


def test_provider_sees_uncertain_marker_before_deploy_returns() -> None:
    provider = _Provider()
    fabric = DeploymentFabric(provider)
    observed: list[DeploymentFabricSnapshot] = []
    provider.observe_before_deploy_result = lambda: observed.append(fabric.snapshot())

    result = fabric.deploy(_intent(EnvironmentTier.STAGING, "stage-pre-dispatch"))

    assert result.state is DeploymentState.HEALTHY
    assert len(observed) == 1
    assert observed[0].records[0].state is DeploymentState.UNCERTAIN
    assert observed[0].records[0].provider_evidence_refs == ()


def test_provider_deploy_exception_is_durable_and_restart_idempotent() -> None:
    provider = _Provider(deploy_error=True)
    fabric = DeploymentFabric(provider)
    intent = _intent(EnvironmentTier.STAGING, "stage-deploy-error")

    uncertain = fabric.deploy(intent)
    assert uncertain.state is DeploymentState.UNCERTAIN
    assert uncertain.provider_evidence_refs == ()
    assert provider.deploy_calls == 1
    assert fabric.snapshot().healthy_staging == ()

    restarted = DeploymentFabric(provider)
    restarted.restore(fabric.snapshot())
    duplicate = restarted.deploy(intent)
    assert duplicate == uncertain
    assert provider.deploy_calls == 1


def test_missing_deploy_evidence_is_durable_and_restart_idempotent() -> None:
    provider = _Provider(deploy_without_evidence=True)
    fabric = DeploymentFabric(provider)
    intent = _intent(EnvironmentTier.STAGING, "stage-missing-deploy-evidence")

    uncertain = fabric.deploy(intent)
    assert uncertain.state is DeploymentState.UNCERTAIN
    assert uncertain.provider_evidence_refs == ()
    assert provider.deploy_calls == 1

    restarted = DeploymentFabric(provider)
    restarted.restore(fabric.snapshot())
    duplicate = restarted.deploy(intent)
    assert duplicate == uncertain
    assert provider.deploy_calls == 1


def test_health_failure_after_applied_effect_is_durable_and_idempotent() -> None:
    provider = _Provider(health_error=True)
    fabric = DeploymentFabric(provider)
    intent = _intent(EnvironmentTier.STAGING, "stage-uncertain")

    uncertain = fabric.deploy(intent)
    assert uncertain.state is DeploymentState.UNCERTAIN
    assert provider.deploy_calls == 1
    assert provider.health_calls == 1
    assert fabric.snapshot().healthy_staging == ()

    duplicate = fabric.deploy(intent)
    assert duplicate == uncertain
    assert provider.deploy_calls == 1

    restarted = DeploymentFabric(provider)
    restarted.restore(fabric.snapshot())
    after_restart = restarted.deploy(intent)
    assert after_restart.state is DeploymentState.UNCERTAIN
    assert provider.deploy_calls == 1


def test_unresolved_staging_effect_invalidates_authority_and_blocks_new_work() -> None:
    provider = _Provider()
    fabric = DeploymentFabric(provider)
    release_a = _intent(EnvironmentTier.STAGING, "stage-a")
    fabric.deploy(release_a)
    assert fabric.snapshot().healthy_staging

    provider.deploy_uncertain = True
    release_b = _intent(
        EnvironmentTier.STAGING,
        "stage-b",
        version="2.0.0",
        digest=DIGEST_B,
    )
    uncertain = fabric.deploy(release_b)
    assert uncertain.state is DeploymentState.UNCERTAIN
    assert fabric.snapshot().healthy_staging == ()
    assert provider.deploy_calls == 2

    with pytest.raises(DeploymentFabricError, match="exact release"):
        fabric.deploy(_intent(EnvironmentTier.PRODUCTION, "prod-a"))
    assert provider.deploy_calls == 2

    with pytest.raises(DeploymentFabricError, match="unresolved deployment effect"):
        fabric.deploy(
            _intent(
                EnvironmentTier.STAGING,
                "stage-c",
                version="3.0.0",
            )
        )
    assert provider.deploy_calls == 2


def test_restore_rejects_stale_authority_over_unresolved_staging_effect() -> None:
    provider = _Provider()
    fabric = DeploymentFabric(provider)
    fabric.deploy(_intent(EnvironmentTier.STAGING, "stage-a"))
    provider.deploy_uncertain = True
    fabric.deploy(
        _intent(
            EnvironmentTier.STAGING,
            "stage-b",
            version="2.0.0",
            digest=DIGEST_B,
        )
    )
    snapshot = fabric.snapshot()
    corrupted = replace(
        snapshot,
        healthy_staging=(("project-1", "1.0.0", SHA_A, DIGEST_A),),
    )

    restarted = DeploymentFabric(provider)
    with pytest.raises(
        DeploymentFabricError,
        match="conflicts with unresolved staging effect",
    ):
        restarted.restore(corrupted)


def test_failed_exact_rollback_stays_uncertain_and_blocks_redeployment() -> None:
    provider = _Provider()
    fabric = DeploymentFabric(provider)
    fabric.deploy(_intent(EnvironmentTier.STAGING, "stage-a"))

    provider.healthy = False
    provider.rollback_succeeds = False
    failed = fabric.deploy(
        _intent(
            EnvironmentTier.STAGING,
            "stage-b",
            version="2.0.0",
            digest=DIGEST_B,
        )
    )
    assert failed.state is DeploymentState.UNCERTAIN
    assert failed.health is not None and not failed.health.healthy
    assert provider.rollback_calls == 1
    assert fabric.snapshot().healthy_staging == ()

    with pytest.raises(DeploymentFabricError, match="unresolved deployment effect"):
        fabric.deploy(
            _intent(
                EnvironmentTier.STAGING,
                "stage-c",
                version="3.0.0",
            )
        )


def test_missing_release_reconciliation_clears_uncertainty_for_next_intent() -> None:
    provider = _Provider(health_error=True)
    fabric = DeploymentFabric(provider)
    uncertain = fabric.deploy(
        _intent(EnvironmentTier.STAGING, "stage-uncertain")
    )
    assert uncertain.state is DeploymentState.UNCERTAIN

    provider.inspection = ProviderInspection(
        None,
        None,
        ("inspect://missing",),
    )
    resolved = fabric.reconcile("stage-uncertain")
    assert resolved.state is DeploymentState.REJECTED

    provider.health_error = False
    provider.inspection = None
    healthy = fabric.deploy(
        _intent(
            EnvironmentTier.STAGING,
            "stage-next",
            version="2.0.0",
            digest=DIGEST_B,
        )
    )
    assert healthy.state is DeploymentState.HEALTHY
