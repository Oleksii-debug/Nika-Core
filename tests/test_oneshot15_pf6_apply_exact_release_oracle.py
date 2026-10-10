from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from nika_core.product_factory_deployment import (
    DeploymentFabric,
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
NOW = datetime(2026, 8, 24, 7, 15, tzinfo=UTC)


def _intent() -> DeploymentIntent:
    release = ReleaseRef("project-oneshot15", "1.0.0", SHA, DIGEST)
    return DeploymentIntent(
        "deploy-apply-exact-oracle",
        "project-oneshot15",
        EnvironmentIdentity(
            "staging-oneshot15",
            "project-oneshot15",
            EnvironmentTier.STAGING,
            "provider://oneshot15",
        ),
        release,
    )


@dataclass
class _AppliedWithoutExactReleaseProvider:
    health_calls: int = 0

    def deploy(self, intent: DeploymentIntent) -> ProviderDeploymentResult:
        del intent
        # Deliberately claim an applied effect without identifying which immutable
        # release was actually applied. SHA/version/digest must not be inferred
        # from the caller's intent after crossing the provider boundary.
        return ProviderDeploymentResult(
            applied=True,
            uncertain=False,
            evidence_refs=("deploy://applied-without-exact-release",),
            release=None,
        )

    def health(self, intent: DeploymentIntent) -> HealthEvidence:
        self.health_calls += 1
        return HealthEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            True,
            ("health://exact-after-ambiguous-apply",),
            NOW,
            release=intent.release,
        )

    def rollback(
        self,
        intent: DeploymentIntent,
        previous_release: ReleaseRef | None,
    ) -> RollbackEvidence:
        raise AssertionError(
            f"rollback must not be reached for ambiguous apply evidence: "
            f"{intent.intent_id} {previous_release}"
        )

    def inspect(self, intent: DeploymentIntent) -> ProviderInspection:
        return ProviderInspection(
            intent.release.source_sha,
            True,
            ("inspect://unused",),
            release=intent.release,
        )


def test_applied_provider_result_without_exact_release_fails_closed_before_health() -> None:
    provider = _AppliedWithoutExactReleaseProvider()
    fabric = DeploymentFabric(provider)

    record = fabric.deploy(_intent())

    assert record.state is DeploymentState.UNCERTAIN, (
        "provider apply evidence must itself bind the exact ReleaseRef; "
        "later exact health cannot retroactively identify an ambiguous applied effect"
    )
    assert provider.health_calls == 0
    assert fabric.snapshot().healthy_staging == ()
