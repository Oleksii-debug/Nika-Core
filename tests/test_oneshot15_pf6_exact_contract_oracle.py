from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import UTC, datetime
from inspect import signature

from nika_core.product_command.deployment_adapter import deployment_status_entries
from nika_core.product_factory_deployment import (
    DeploymentFabric,
    DeploymentFabricError,
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
NOW = datetime(2026, 8, 23, 21, 30, tzinfo=UTC)


def _release(version: str, digest: str) -> ReleaseRef:
    return ReleaseRef("project-oneshot15", version, SHA, digest)


def _intent(intent_id: str, release: ReleaseRef) -> DeploymentIntent:
    return DeploymentIntent(
        intent_id,
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
class _ExactInspectionProvider:
    current: ReleaseRef | None = None
    first_release: ReleaseRef | None = None
    deploy_calls: int = 0

    def deploy(self, intent: DeploymentIntent) -> ProviderDeploymentResult:
        self.deploy_calls += 1
        if self.first_release is None:
            self.first_release = intent.release
            self.current = intent.release
            return ProviderDeploymentResult(True, False, (f"deploy://{intent.intent_id}",))
        # Model an uncertain second dispatch whose externally observable final state
        # is already the exact previous release. Reconciliation must not redispatch.
        self.current = self.first_release
        return ProviderDeploymentResult(False, True, (f"deploy://{intent.intent_id}",))

    def health(self, intent: DeploymentIntent) -> HealthEvidence:
        return HealthEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            True,
            (f"health://{intent.intent_id}",),
            NOW,
            release=intent.release,
        )

    def rollback(
        self,
        intent: DeploymentIntent,
        previous_release_sha: str | None,
    ) -> RollbackEvidence:
        raise AssertionError(
            f"reconcile must not blindly redispatch rollback for {intent.intent_id}: "
            f"{previous_release_sha}"
        )

    def inspect(self, intent: DeploymentIntent) -> ProviderInspection:
        del intent
        if self.current is None:
            return ProviderInspection(None, None, ("inspect://none",))
        return ProviderInspection(
            self.current.source_sha,
            True,
            ("inspect://exact-current",),
            release=self.current,
        )


@dataclass
class _ShaOnlyHealthProvider:
    current: ReleaseRef | None = None

    def deploy(self, intent: DeploymentIntent) -> ProviderDeploymentResult:
        self.current = intent.release
        return ProviderDeploymentResult(True, False, (f"deploy://{intent.intent_id}",))

    def health(self, intent: DeploymentIntent) -> HealthEvidence:
        # Deliberately omit exact ReleaseRef. SHA equality is insufficient because
        # multiple immutable artifacts may share one source SHA.
        return HealthEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            True,
            (f"health://{intent.intent_id}:sha-only",),
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
            False,
            (f"rollback://{intent.intent_id}:unused",),
        )

    def inspect(self, intent: DeploymentIntent) -> ProviderInspection:
        del intent
        if self.current is None:
            return ProviderInspection(None, None, ("inspect://none",))
        return ProviderInspection(
            self.current.source_sha,
            True,
            ("inspect://sha-only",),
        )


def test_canonical_provider_contract_uses_exact_release_identity() -> None:
    deployment_result_fields = {field.name for field in fields(ProviderDeploymentResult)}
    rollback_parameters = signature(DeploymentProviderPort.rollback).parameters

    assert "release" in deployment_result_fields, (
        "provider apply evidence must carry the exact ReleaseRef rather than only booleans/evidence refs"
    )
    assert "previous_release" in rollback_parameters, (
        "canonical rollback must receive the exact previous ReleaseRef"
    )
    assert "previous_release_sha" not in rollback_parameters, (
        "SHA-only rollback authority must be legacy migration data, not the canonical provider port"
    )


def test_sha_only_health_evidence_cannot_make_exact_release_healthy() -> None:
    release = _release("1.0.0", DIGEST_A)
    fabric = DeploymentFabric(_ShaOnlyHealthProvider())

    try:
        record = fabric.deploy(_intent("deploy-a", release))
    except DeploymentFabricError:
        return

    assert record.state is not DeploymentState.HEALTHY, (
        "SHA-only health evidence must fail closed instead of granting exact-release health authority"
    )


def test_uncertain_reconcile_accepts_exact_previous_release_without_replay() -> None:
    release_a = _release("1.0.0", DIGEST_A)
    release_b = _release("2.0.0", DIGEST_B)
    provider = _ExactInspectionProvider()
    fabric = DeploymentFabric(provider)

    first = fabric.deploy(_intent("deploy-a", release_a))
    uncertain = fabric.deploy(_intent("deploy-b", release_b))

    assert first.state is DeploymentState.HEALTHY
    assert uncertain.state is DeploymentState.UNCERTAIN
    assert uncertain.previous_release == release_a
    before_calls = provider.deploy_calls

    reconciled = fabric.reconcile("deploy-b")

    assert reconciled.state in {DeploymentState.REJECTED, DeploymentState.ROLLED_BACK}
    assert provider.deploy_calls == before_calls
    assert fabric.snapshot().current_releases == (
        (
            "project-oneshot15",
            "staging-oneshot15",
            release_a.version,
            release_a.source_sha,
            release_a.artifact_digest,
        ),
    )


def test_integrated_product_command_consumes_exact_release_snapshot() -> None:
    release = _release("1.0.0", DIGEST_A)
    provider = _ExactInspectionProvider()
    fabric = DeploymentFabric(provider)

    record = fabric.deploy(_intent("deploy-a", release))
    assert record.state is DeploymentState.HEALTHY
    snapshot = fabric.snapshot()
    assert snapshot.healthy_staging == (
        ("project-oneshot15", release.version, release.source_sha, release.artifact_digest),
    )
    assert snapshot.current_releases == (
        (
            "project-oneshot15",
            "staging-oneshot15",
            release.version,
            release.source_sha,
            release.artifact_digest,
        ),
    )

    entries = deployment_status_entries(snapshot)

    assert entries
    assert any(entry.item_id == "release:deploy-a" for entry in entries)
