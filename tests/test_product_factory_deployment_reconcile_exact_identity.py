from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nika_core.product_factory_deployment import (
    DeploymentFabric,
    DeploymentFabricError,
    DeploymentIntent,
    DeploymentState,
    EnvironmentIdentity,
    EnvironmentTier,
    HealthEvidence,
    ProviderDeploymentResult,
    ProviderInspection,
    ReleaseRef,
    RollbackEvidence,
)

SHA = "a" * 40
DIGEST = "1" * 64


class IncompleteInspectionProvider:
    def deploy(self, intent: DeploymentIntent) -> ProviderDeploymentResult:
        return ProviderDeploymentResult(True, False, ("deploy://applied",))

    def health(self, intent: DeploymentIntent) -> HealthEvidence:
        raise RuntimeError("synthetic health transport failure")

    def rollback(
        self, intent: DeploymentIntent, previous_release_sha: str | None
    ) -> RollbackEvidence:
        raise AssertionError("rollback is not expected for an uncertain deployment")

    def inspect(self, intent: DeploymentIntent) -> ProviderInspection:
        return ProviderInspection(
            intent.release.source_sha,
            True,
            ("inspect://sha-only",),
            release=None,
        )


def _intent() -> DeploymentIntent:
    release = ReleaseRef("p1", "1.0.0", SHA, DIGEST)
    environment = EnvironmentIdentity(
        "staging-1",
        "p1",
        EnvironmentTier.STAGING,
        "provider://fake",
    )
    return DeploymentIntent("deploy:p1:1", "p1", environment, release)


def test_reconciliation_rejects_sha_only_identity_and_preserves_uncertainty() -> None:
    fabric = DeploymentFabric(IncompleteInspectionProvider())
    intent = _intent()

    initial = fabric.deploy(intent)
    assert initial.state is DeploymentState.UNCERTAIN

    with pytest.raises(DeploymentFabricError, match="exact release"):
        fabric.reconcile(intent.intent_id)

    snapshot = fabric.snapshot()
    assert snapshot.records[0].state is DeploymentState.UNCERTAIN
    assert snapshot.current_releases == ()
    assert snapshot.exact_current_releases == ()
    assert snapshot.healthy_staging == ()
    assert snapshot.exact_healthy_staging == ()
