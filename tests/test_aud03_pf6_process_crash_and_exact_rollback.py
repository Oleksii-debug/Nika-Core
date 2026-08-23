from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from nika_core.product_factory_deployment import (
    DeploymentFabric,
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

SHA = "a" * 40
DIGEST_A = "1" * 64
DIGEST_B = "2" * 64
NOW = datetime(2026, 8, 23, 20, 0, tzinfo=UTC)


@dataclass
class _CrashOnceProvider(DeploymentProviderPort):
    calls: int = 0
    current: ReleaseRef | None = None

    def deploy(self, intent: DeploymentIntent) -> ProviderDeploymentResult:
        self.calls += 1
        self.current = intent.release
        if self.calls == 1:
            raise SystemExit("simulated process crash after provider mutation")
        return ProviderDeploymentResult(True, False, ("deploy://second-dispatch",))

    def health(self, intent: DeploymentIntent) -> HealthEvidence:
        return HealthEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            True,
            ("health://aud03",),
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
            ("rollback://aud03",),
        )

    def inspect(self, intent: DeploymentIntent) -> ProviderInspection:
        del intent
        if self.current is None:
            return ProviderInspection(None, None, ("inspect://none",))
        return ProviderInspection(self.current.source_sha, True, ("inspect://current",))


@dataclass
class _ShaOnlyRollbackProvider(DeploymentProviderPort):
    current: ReleaseRef | None = None
    releases_by_sha: dict[str, list[ReleaseRef]] = field(default_factory=dict)

    def deploy(self, intent: DeploymentIntent) -> ProviderDeploymentResult:
        self.releases_by_sha.setdefault(intent.release.source_sha, []).append(intent.release)
        self.current = intent.release
        return ProviderDeploymentResult(True, False, ("deploy://aud03",))

    def health(self, intent: DeploymentIntent) -> HealthEvidence:
        return HealthEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            intent.release.version == "1.0.0",
            ("health://aud03",),
            NOW,
        )

    def rollback(
        self,
        intent: DeploymentIntent,
        previous_release_sha: str | None,
    ) -> RollbackEvidence:
        if previous_release_sha is not None:
            self.current = self.releases_by_sha[previous_release_sha][-1]
        return RollbackEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            previous_release_sha,
            True,
            ("rollback://aud03",),
        )

    def inspect(self, intent: DeploymentIntent) -> ProviderInspection:
        del intent
        if self.current is None:
            return ProviderInspection(None, None, ("inspect://none",))
        return ProviderInspection(self.current.source_sha, True, ("inspect://current",))


def _staging() -> EnvironmentIdentity:
    return EnvironmentIdentity(
        "staging-aud03",
        "project-aud03",
        EnvironmentTier.STAGING,
        "provider://aud03",
    )


def test_process_crash_after_provider_effect_cannot_redispatch_same_intent() -> None:
    provider = _CrashOnceProvider()
    intent = DeploymentIntent(
        "deploy-crash",
        "project-aud03",
        _staging(),
        ReleaseRef("project-aud03", "1.0.0", SHA, DIGEST_A),
    )

    first_process = DeploymentFabric(provider)
    with pytest.raises(SystemExit, match="simulated process crash"):
        first_process.deploy(intent)
    assert provider.calls == 1
    assert provider.current == intent.release

    restarted_process = DeploymentFabric(provider)
    restarted_process.deploy(intent)

    assert provider.calls == 1, (
        "same deployment intent was dispatched again after process loss because the "
        "pre-dispatch UNCERTAIN marker had no durable restart host"
    )


def test_rollback_success_requires_exact_previous_artifact_identity() -> None:
    provider = _ShaOnlyRollbackProvider()
    fabric = DeploymentFabric(provider)
    release_a = ReleaseRef("project-aud03", "1.0.0", SHA, DIGEST_A)
    release_b = ReleaseRef("project-aud03", "2.0.0", SHA, DIGEST_B)

    first = fabric.deploy(
        DeploymentIntent("deploy-a", "project-aud03", _staging(), release_a)
    )
    assert first.state is DeploymentState.HEALTHY
    assert provider.current == release_a

    second = fabric.deploy(
        DeploymentIntent("deploy-b", "project-aud03", _staging(), release_b)
    )
    assert second.state is DeploymentState.ROLLED_BACK
    assert provider.current == release_a, (
        "PF6 accepted rollback success using only previous source SHA; the provider "
        "restored a different artifact/version sharing that SHA"
    )
