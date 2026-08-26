from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest

from nika_core.product_factory_deployment import (
    DeploymentFabric,
    DeploymentFabricError,
    DeploymentIntent,
    DeploymentProviderPort,
    DeploymentState,
    EnvironmentIdentity,
    EnvironmentTier,
    ExecutionNode,
    ExecutionNodeRegistry,
    ExecutionRequest,
    HealthEvidence,
    NodeCapabilities,
    NodeIdentity,
    Platform,
    ProviderDeploymentResult,
    ProviderInspection,
    ReleaseRef,
    ResourceEnvelope,
    RollbackEvidence,
    local_linux_node,
    local_windows_node,
)

SHA_A = "a" * 40
SHA_B = "b" * 40
DIGEST_A = "1" * 64
NOW = datetime(2026, 8, 20, 10, 0, tzinfo=UTC)


@dataclass
class FakeDeploymentProvider(DeploymentProviderPort):
    deploy_result: ProviderDeploymentResult
    healthy: bool = True
    inspection: ProviderInspection | None = None
    deploy_calls: int = 0
    rollback_calls: int = 0

    def deploy(self, intent: DeploymentIntent) -> ProviderDeploymentResult:
        self.deploy_calls += 1
        if self.deploy_result.applied and self.deploy_result.release is None:
            return replace(self.deploy_result, release=intent.release)
        return self.deploy_result

    def health(self, intent: DeploymentIntent) -> HealthEvidence:
        return HealthEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            self.healthy,
            ("health://fake",),
            NOW,
            release=intent.release,
        )

    def rollback(
        self,
        intent: DeploymentIntent,
        previous_release: ReleaseRef | None,
    ) -> RollbackEvidence:
        self.rollback_calls += 1
        return RollbackEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            previous_release.source_sha if previous_release is not None else None,
            True,
            ("rollback://fake",),
            failed_release=intent.release,
            restored_release=previous_release,
        )

    def inspect(self, intent: DeploymentIntent) -> ProviderInspection:
        if self.inspection is None:
            return ProviderInspection(
                intent.release.source_sha,
                True,
                ("inspect://fake",),
                release=intent.release,
            )
        return self.inspection


def _release(sha: str = SHA_A) -> ReleaseRef:
    return ReleaseRef("project-1", "1.0.0", sha, DIGEST_A)


def _env(tier: EnvironmentTier) -> EnvironmentIdentity:
    return EnvironmentIdentity(f"env-{tier.value}", "project-1", tier, "provider://fake")


def _intent(tier: EnvironmentTier, *, intent_id: str, sha: str = SHA_A) -> DeploymentIntent:
    return DeploymentIntent(intent_id, "project-1", _env(tier), _release(sha))


def test_local_node_profiles_and_platform_routing() -> None:
    registry = ExecutionNodeRegistry()
    registry.register(local_windows_node())
    registry.register(local_linux_node())

    windows = registry.acquire(
        ExecutionRequest(
            "project-1",
            "work-win",
            Platform.WINDOWS,
            frozenset({"package"}),
            frozenset({"python"}),
            ResourceEnvelope(1, 512, 512),
        ),
        now=NOW,
    )
    linux = registry.acquire(
        ExecutionRequest(
            "project-1",
            "work-linux",
            Platform.LINUX,
            frozenset({"container"}),
            frozenset({"bash"}),
            ResourceEnvelope(1, 512, 512),
        ),
        now=NOW,
    )

    assert windows.node_id == "local-windows"
    assert linux.node_id == "local-linux"


def test_node_registry_reroutes_busy_matching_node() -> None:
    registry = ExecutionNodeRegistry()
    registry.register(local_linux_node())
    registry.register(
        ExecutionNode(
            NodeIdentity("local-linux-2", Platform.LINUX, "x86_64", "local-linux-2"),
            NodeCapabilities(
                frozenset({"local", "container"}), frozenset({"python", "bash"})
            ),
            ResourceEnvelope(2, 2048, 4096),
        )
    )
    first_request = ExecutionRequest(
        "project-1",
        "work-1",
        Platform.LINUX,
        frozenset({"container"}),
        frozenset({"python"}),
        ResourceEnvelope(1, 512, 512),
    )
    second_request = ExecutionRequest(
        "project-1",
        "work-2",
        Platform.LINUX,
        first_request.required_features,
        first_request.required_toolchains,
        first_request.resources,
    )

    first = registry.acquire(first_request, now=NOW)
    second = registry.acquire(second_request, now=NOW)

    assert first.node_id == "local-linux"
    assert second.node_id == "local-linux-2"


def test_unavailable_platform_fails_closed() -> None:
    registry = ExecutionNodeRegistry()
    registry.register(local_windows_node())

    with pytest.raises(DeploymentFabricError, match="no available execution node"):
        registry.acquire(
            ExecutionRequest(
                "project-1",
                "work-macos",
                Platform.MACOS,
                frozenset(),
                frozenset({"xcode"}),
                ResourceEnvelope(1, 512, 512),
            ),
            now=NOW,
        )


def test_node_snapshot_restore_preserves_lease_counter() -> None:
    registry = ExecutionNodeRegistry()
    registry.register(local_windows_node())
    lease = registry.acquire(
        ExecutionRequest(
            "project-1",
            "work-1",
            Platform.WINDOWS,
            frozenset(),
            frozenset({"python"}),
            ResourceEnvelope(1, 512, 512),
        ),
        now=NOW,
        lease_seconds=60,
    )
    snapshot = registry.snapshot()

    restored = ExecutionNodeRegistry()
    restored.restore(snapshot)
    restored.release(lease.lease_id)
    next_lease = restored.acquire(
        ExecutionRequest(
            "project-1",
            "work-2",
            Platform.WINDOWS,
            frozenset(),
            frozenset({"python"}),
            ResourceEnvelope(1, 512, 512),
        ),
        now=NOW + timedelta(seconds=1),
    )

    assert next_lease.lease_id == "lease-00000002"


def test_production_requires_healthy_staging_for_exact_sha() -> None:
    provider = FakeDeploymentProvider(ProviderDeploymentResult(True, False, ("deploy://ok",)))
    fabric = DeploymentFabric(provider)

    with pytest.raises(DeploymentFabricError, match="healthy staging proof"):
        fabric.deploy(_intent(EnvironmentTier.PRODUCTION, intent_id="prod-1"))

    staging = fabric.deploy(_intent(EnvironmentTier.STAGING, intent_id="stage-1"))
    production = fabric.deploy(_intent(EnvironmentTier.PRODUCTION, intent_id="prod-1"))

    assert staging.state is DeploymentState.HEALTHY
    assert production.state is DeploymentState.HEALTHY


def test_production_rejects_different_sha_than_staging() -> None:
    provider = FakeDeploymentProvider(ProviderDeploymentResult(True, False, ("deploy://ok",)))
    fabric = DeploymentFabric(provider)
    fabric.deploy(_intent(EnvironmentTier.STAGING, intent_id="stage-1", sha=SHA_A))

    with pytest.raises(DeploymentFabricError, match="healthy staging proof"):
        fabric.deploy(_intent(EnvironmentTier.PRODUCTION, intent_id="prod-1", sha=SHA_B))


def test_bad_health_rolls_back() -> None:
    provider = FakeDeploymentProvider(
        ProviderDeploymentResult(True, False, ("deploy://ok",)),
        healthy=False,
    )
    fabric = DeploymentFabric(provider)

    record = fabric.deploy(_intent(EnvironmentTier.STAGING, intent_id="stage-bad"))

    assert record.state is DeploymentState.ROLLED_BACK
    assert record.health is not None and not record.health.healthy
    assert record.rollback is not None and record.rollback.succeeded
    assert provider.rollback_calls == 1


def test_duplicate_deployment_is_idempotent() -> None:
    provider = FakeDeploymentProvider(ProviderDeploymentResult(True, False, ("deploy://ok",)))
    fabric = DeploymentFabric(provider)
    intent = _intent(EnvironmentTier.STAGING, intent_id="stage-1")

    first = fabric.deploy(intent)
    second = fabric.deploy(intent)

    assert first == second
    assert provider.deploy_calls == 1


def test_duplicate_intent_id_with_changed_payload_fails_closed() -> None:
    provider = FakeDeploymentProvider(ProviderDeploymentResult(True, False, ("deploy://ok",)))
    fabric = DeploymentFabric(provider)
    fabric.deploy(_intent(EnvironmentTier.STAGING, intent_id="stage-1", sha=SHA_A))

    with pytest.raises(DeploymentFabricError, match="conflicts with prior payload"):
        fabric.deploy(_intent(EnvironmentTier.STAGING, intent_id="stage-1", sha=SHA_B))


def test_uncertain_deployment_reconciles_to_healthy() -> None:
    provider = FakeDeploymentProvider(
        ProviderDeploymentResult(
            applied=False, uncertain=True, evidence_refs=("deploy://timeout",)
        ),
        inspection=ProviderInspection(
            SHA_A,
            True,
            ("inspect://healthy",),
            release=_release(SHA_A),
        ),
    )
    fabric = DeploymentFabric(provider)
    intent = _intent(EnvironmentTier.STAGING, intent_id="stage-uncertain")

    uncertain = fabric.deploy(intent)
    reconciled = fabric.reconcile(intent.intent_id)

    assert uncertain.state is DeploymentState.UNCERTAIN
    assert reconciled.state is DeploymentState.HEALTHY


def test_uncertain_missing_deployment_reconciles_to_rejected() -> None:
    provider = FakeDeploymentProvider(
        ProviderDeploymentResult(
            applied=False, uncertain=True, evidence_refs=("deploy://timeout",)
        ),
        inspection=ProviderInspection(None, None, ("inspect://missing",)),
    )
    fabric = DeploymentFabric(provider)
    intent = _intent(EnvironmentTier.STAGING, intent_id="stage-uncertain")

    fabric.deploy(intent)
    reconciled = fabric.reconcile(intent.intent_id)

    assert reconciled.state is DeploymentState.REJECTED


def test_deployment_snapshot_restart_preserves_idempotency_and_staging_proof() -> None:
    provider = FakeDeploymentProvider(ProviderDeploymentResult(True, False, ("deploy://ok",)))
    fabric = DeploymentFabric(provider)
    staging_intent = _intent(EnvironmentTier.STAGING, intent_id="stage-1")
    fabric.deploy(staging_intent)
    snapshot = fabric.snapshot()

    restarted = DeploymentFabric(provider)
    restarted.restore(snapshot)
    duplicate = restarted.deploy(staging_intent)
    production = restarted.deploy(_intent(EnvironmentTier.PRODUCTION, intent_id="prod-1"))

    assert duplicate.state is DeploymentState.HEALTHY
    assert production.state is DeploymentState.HEALTHY
    assert provider.deploy_calls == 2


def test_release_sha_is_exact_and_lowercase_hex() -> None:
    with pytest.raises(DeploymentFabricError, match="40-character"):
        ReleaseRef("project-1", "1.0.0", "not-a-sha", DIGEST_A)
    with pytest.raises(DeploymentFabricError, match="40-character"):
        ReleaseRef("project-1", "1.0.0", "A" * 40, DIGEST_A)
