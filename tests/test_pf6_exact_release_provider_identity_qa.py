from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import UTC, datetime

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
DIGEST_A = "1" * 64
DIGEST_B = "2" * 64
NOW = datetime(2026, 8, 24, 18, 0, tzinfo=UTC)


def _intent(
    intent_id: str,
    *,
    version: str = "1.0.0",
    digest: str = DIGEST_A,
) -> DeploymentIntent:
    release = ReleaseRef("project-qa-pf6", version, SHA, digest)
    return DeploymentIntent(
        intent_id,
        "project-qa-pf6",
        EnvironmentIdentity(
            "staging-qa-pf6",
            "project-qa-pf6",
            EnvironmentTier.STAGING,
            "provider://qa-pf6",
        ),
        release,
    )


def _deployment_result(
    intent: DeploymentIntent,
    *,
    report_release: bool,
) -> ProviderDeploymentResult:
    kwargs: dict[str, object] = {
        "applied": True,
        "uncertain": False,
        "evidence_refs": ("deploy://qa",),
    }
    names = {item.name for item in fields(ProviderDeploymentResult)}
    for candidate in (
        "release",
        "applied_release",
        "release_ref",
        "applied_release_ref",
    ):
        if candidate in names:
            kwargs[candidate] = intent.release if report_release else None
            break
    return ProviderDeploymentResult(**kwargs)


@dataclass
class _MissingAppliedReleaseProvider:
    health_calls: int = 0

    def deploy(self, intent: DeploymentIntent) -> ProviderDeploymentResult:
        return _deployment_result(intent, report_release=False)

    def health(self, intent: DeploymentIntent) -> HealthEvidence:
        self.health_calls += 1
        return HealthEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            True,
            ("health://exact",),
            NOW,
            release=intent.release,
        )

    def rollback(
        self,
        intent: DeploymentIntent,
        previous_release_sha: str | None,
    ) -> RollbackEvidence:
        raise AssertionError("rollback must not run")

    def inspect(self, intent: DeploymentIntent) -> ProviderInspection:
        raise AssertionError("inspect must not run")


def test_applied_effect_without_provider_reported_exact_release_is_not_authorized() -> None:
    provider = _MissingAppliedReleaseProvider()
    fabric = DeploymentFabric(provider)

    record = fabric.deploy(_intent("missing-applied-release"))

    assert record.state is DeploymentState.UNCERTAIN
    assert provider.health_calls == 0
    assert fabric.snapshot().healthy_staging == ()


@dataclass
class _ShaOnlyHealthProvider:
    def deploy(self, intent: DeploymentIntent) -> ProviderDeploymentResult:
        return _deployment_result(intent, report_release=True)

    def health(self, intent: DeploymentIntent) -> HealthEvidence:
        return HealthEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            True,
            ("health://sha-only",),
            NOW,
            release=None,
        )

    def rollback(
        self,
        intent: DeploymentIntent,
        previous_release_sha: str | None,
    ) -> RollbackEvidence:
        raise AssertionError("rollback must not run")

    def inspect(self, intent: DeploymentIntent) -> ProviderInspection:
        raise AssertionError("inspect must not run")


def test_sha_only_health_cannot_create_exact_release_authority() -> None:
    fabric = DeploymentFabric(_ShaOnlyHealthProvider())
    intent = _intent("sha-only-health")

    record = fabric.deploy(intent)

    assert record.state is DeploymentState.UNCERTAIN
    assert fabric.snapshot().healthy_staging == ()
    assert fabric.snapshot().current_releases == ()


@dataclass
class _ShaOnlyInspectionProvider:
    def deploy(self, intent: DeploymentIntent) -> ProviderDeploymentResult:
        raise RuntimeError("effect may have happened")

    def health(self, intent: DeploymentIntent) -> HealthEvidence:
        raise AssertionError("health must not run")

    def rollback(
        self,
        intent: DeploymentIntent,
        previous_release_sha: str | None,
    ) -> RollbackEvidence:
        raise AssertionError("rollback must not run")

    def inspect(self, intent: DeploymentIntent) -> ProviderInspection:
        return ProviderInspection(
            intent.release.source_sha,
            True,
            ("inspect://sha-only",),
            release=None,
        )


def test_sha_only_inspection_cannot_resolve_uncertain_effect_to_healthy() -> None:
    provider = _ShaOnlyInspectionProvider()
    fabric = DeploymentFabric(provider)
    intent = _intent("sha-only-inspect")
    uncertain = fabric.deploy(intent)
    assert uncertain.state is DeploymentState.UNCERTAIN

    try:
        reconciled = fabric.reconcile(intent.intent_id)
    except DeploymentFabricError:
        reconciled = fabric.snapshot().records[0]

    assert reconciled.state is DeploymentState.UNCERTAIN
    assert fabric.snapshot().healthy_staging == ()
    assert fabric.snapshot().current_releases == ()


@dataclass
class _RollbackResultMissingFailedReleaseProvider:
    current: ReleaseRef | None = None

    def deploy(self, intent: DeploymentIntent) -> ProviderDeploymentResult:
        self.current = intent.release
        return _deployment_result(intent, report_release=True)

    def health(self, intent: DeploymentIntent) -> HealthEvidence:
        return HealthEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            intent.release.version == "1.0.0",
            ("health://exact",),
            NOW,
            release=intent.release,
        )

    def rollback(
        self,
        intent: DeploymentIntent,
        previous_release_sha: str | None,
    ) -> RollbackEvidence:
        raise AssertionError("legacy rollback must not run")

    def rollback_exact(
        self,
        intent: DeploymentIntent,
        previous_release: ReleaseRef | None,
    ) -> RollbackEvidence:
        self.current = previous_release
        return RollbackEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            previous_release.source_sha if previous_release is not None else None,
            True,
            ("rollback://missing-failed-release",),
            failed_release=None,
            restored_release=previous_release,
        )

    def inspect(self, intent: DeploymentIntent) -> ProviderInspection:
        raise AssertionError("inspect must not run")


def test_exact_rollback_result_must_bind_failed_and_restored_release_refs() -> None:
    provider = _RollbackResultMissingFailedReleaseProvider()
    fabric = DeploymentFabric(provider)
    first = _intent("release-a")
    second = _intent("release-b", version="2.0.0", digest=DIGEST_B)

    assert fabric.deploy(first).state is DeploymentState.HEALTHY
    result = fabric.deploy(second)

    assert result.state is DeploymentState.UNCERTAIN
    assert fabric.snapshot().healthy_staging == ()
