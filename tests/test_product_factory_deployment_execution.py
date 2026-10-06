from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from nika_core.product_factory_credentials import CredentialBroker, SecretRef
from nika_core.product_factory_deployment import (
    DeploymentFabric,
    DeploymentIntent,
    DeploymentState,
    EnvironmentIdentity,
    EnvironmentTier,
    ExecutionNodeRegistry,
    ExecutionRequest,
    HealthEvidence,
    Platform,
    ProviderDeploymentResult,
    ProviderInspection,
    ReleaseRef,
    ResourceEnvelope,
    RollbackEvidence,
    local_linux_node,
)
from nika_core.product_factory_deployment_execution import (
    DeploymentExecutionCoordinator,
    DeploymentExecutionError,
    DeploymentExecutionSnapshot,
    DeploymentExecutionSpec,
    OperationState,
)

NOW = datetime(2026, 8, 21, 3, 0, tzinfo=UTC)
SHA1 = "1" * 40
SHA2 = "2" * 40
DIGEST = "a" * 64


@dataclass
class FakeProtectedStore:
    generations: set[tuple[str, int]] = field(default_factory=set)

    def contains(self, secret_ref: str, generation: int) -> bool:
        return (secret_ref, generation) in self.generations

    def issue_handle(self, **kwargs: object) -> str:
        return f"opaque-handle:{kwargs['secret_ref']}:{kwargs['generation']}"

    def revoke_handles(self, secret_ref: str, generation: int) -> None:
        return None


@dataclass
class FakeNodeHealth:
    available: bool = True

    def is_available(self, node_id: str) -> bool:
        assert node_id
        return self.available


@dataclass
class FakeProvider:
    uncertain_intents: set[str] = field(default_factory=set)
    inspected_healthy: bool | None = True
    deploy_calls: list[str] = field(default_factory=list)

    def deploy(self, intent: DeploymentIntent) -> ProviderDeploymentResult:
        self.deploy_calls.append(intent.intent_id)
        return ProviderDeploymentResult(
            applied=True,
            uncertain=intent.intent_id in self.uncertain_intents,
            evidence_refs=(f"provider:deploy:{intent.intent_id}",),
        )

    def health(self, intent: DeploymentIntent) -> HealthEvidence:
        return HealthEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            True,
            (f"provider:health:{intent.intent_id}",),
            NOW,
        )

    def rollback(
        self, intent: DeploymentIntent, previous_release_sha: str | None
    ) -> RollbackEvidence:
        return RollbackEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            previous_release_sha,
            True,
            (f"provider:rollback:{intent.intent_id}",),
        )

    def inspect(self, intent: DeploymentIntent) -> ProviderInspection:
        return ProviderInspection(
            intent.release.source_sha,
            self.inspected_healthy,
            (f"provider:inspect:{intent.intent_id}",),
        )


def _intent(project: str, service: str, sha: str = SHA1) -> DeploymentIntent:
    return DeploymentIntent(
        intent_id=f"deploy-{project}-{service}-{sha[:4]}",
        project_id=project,
        environment=EnvironmentIdentity(
            environment_id="shared-staging",
            project_id=project,
            tier=EnvironmentTier.STAGING,
            provider_ref="provider:fake-staging",
        ),
        release=ReleaseRef(project, f"1.0-{service}", sha, DIGEST),
    )


def _spec(project: str, service: str, sha: str = SHA1) -> DeploymentExecutionSpec:
    return DeploymentExecutionSpec(
        operation_id=f"operation-{project}-{service}-{sha[:4]}",
        request=ExecutionRequest(
            project_id=project,
            work_id=f"work-{project}-{service}",
            platform=Platform.LINUX,
            required_features=frozenset(),
            required_toolchains=frozenset(),
            resources=ResourceEnvelope(1, 256, 256),
        ),
        intent=_intent(project, service, sha),
        credential_ref=f"secret:{project}",
        credential_audience="staging-provider",
        credential_scope="deploy:staging",
    )


def _coordinator(
    *, provider: FakeProvider | None = None, health: FakeNodeHealth | None = None
) -> tuple[DeploymentExecutionCoordinator, CredentialBroker, FakeProvider, FakeNodeHealth]:
    nodes = ExecutionNodeRegistry()
    nodes.register(local_linux_node())
    store = FakeProtectedStore({("secret:project-a", 1)})
    credentials = CredentialBroker(store)
    credentials.register_secret(
        SecretRef(
            "secret:project-a",
            "project-a",
            "fake-staging",
            "staging deployment",
            frozenset({"deploy:staging"}),
            frozenset({"staging-provider"}),
        ),
        now=NOW,
    )
    actual_provider = provider or FakeProvider()
    actual_health = health or FakeNodeHealth()
    coordinator = DeploymentExecutionCoordinator(
        nodes,
        credentials,
        DeploymentFabric(actual_provider),
        actual_health,
    )
    return coordinator, credentials, actual_provider, actual_health


def test_execution_success_releases_ephemeral_leases() -> None:
    coordinator, _, provider, _ = _coordinator()
    spec = _spec("project-a", "messages")

    assert coordinator.submit(spec, now=NOW).state is OperationState.PENDING
    prepared = coordinator.prepare(spec.operation_id, now=NOW)
    assert prepared.state is OperationState.PREPARED
    assert prepared.node_id == "local-linux"

    completed = coordinator.complete(spec.operation_id, now=NOW)
    assert completed.state is OperationState.SUCCEEDED
    assert completed.deployment_state is DeploymentState.HEALTHY
    assert provider.deploy_calls == [spec.intent.intent_id]
    assert any(ref.startswith("execution-node:") for ref in completed.evidence_refs)
    assert sum(ref.startswith("credential-use:") for ref in completed.evidence_refs) == 2

    # Node lease is released after the provider boundary completes.
    second = _spec("project-a", "profiles", SHA2)
    coordinator.submit(second, now=NOW)
    assert coordinator.prepare(second.operation_id, now=NOW).state is OperationState.PREPARED


def test_node_loss_before_provider_call_waits_without_mutation() -> None:
    health = FakeNodeHealth()
    coordinator, _, provider, _ = _coordinator(health=health)
    spec = _spec("project-a", "messages")
    coordinator.submit(spec, now=NOW)
    assert coordinator.prepare(spec.operation_id, now=NOW).state is OperationState.PREPARED

    health.available = False
    waiting = coordinator.complete(spec.operation_id, now=NOW)

    assert waiting.state is OperationState.WAITING_FOR_NODE
    assert waiting.node_id is None
    assert provider.deploy_calls == []

    health.available = True
    assert coordinator.retry(spec.operation_id, now=NOW).state is OperationState.PREPARED
    assert coordinator.complete(spec.operation_id, now=NOW).state is OperationState.SUCCEEDED


def test_credential_revocation_mid_operation_blocks_before_provider() -> None:
    coordinator, credentials, provider, _ = _coordinator()
    spec = _spec("project-a", "messages")
    coordinator.submit(spec, now=NOW)
    assert coordinator.prepare(spec.operation_id, now=NOW).state is OperationState.PREPARED

    credentials.revoke(project_id="project-a", secret_ref="secret:project-a", now=NOW)
    blocked = coordinator.complete(spec.operation_id, now=NOW)

    assert blocked.state is OperationState.BLOCKED_CREDENTIAL
    assert provider.deploy_calls == []


def test_restart_drops_ephemeral_leases_and_requires_recovery() -> None:
    coordinator, _, provider, health = _coordinator()
    spec = _spec("project-a", "messages")
    coordinator.submit(spec, now=NOW)
    assert coordinator.prepare(spec.operation_id, now=NOW).state is OperationState.PREPARED

    snapshot = coordinator.snapshot()
    persisted = snapshot.records[0]
    assert persisted.state is OperationState.RECOVERY_REQUIRED
    assert persisted.node_id is None

    restored, _, restored_provider, _ = _coordinator(provider=provider, health=health)
    restored.restore(snapshot)
    assert restored.get(spec.operation_id).state is OperationState.RECOVERY_REQUIRED
    assert restored.retry(spec.operation_id, now=NOW).state is OperationState.PREPARED
    assert restored.complete(spec.operation_id, now=NOW).state is OperationState.SUCCEEDED
    assert restored_provider.deploy_calls == [spec.intent.intent_id]


def test_uncertain_deployment_requires_inspection_not_blind_replay() -> None:
    spec = _spec("project-a", "messages")
    provider = FakeProvider(uncertain_intents={spec.intent.intent_id})
    coordinator, _, _, _ = _coordinator(provider=provider)
    coordinator.submit(spec, now=NOW)
    coordinator.prepare(spec.operation_id, now=NOW)

    uncertain = coordinator.complete(spec.operation_id, now=NOW)
    assert uncertain.state is OperationState.RECONCILE_REQUIRED
    assert uncertain.deployment_state is DeploymentState.UNCERTAIN
    assert provider.deploy_calls == [spec.intent.intent_id]

    reconciled = coordinator.reconcile(spec.operation_id, now=NOW)
    assert reconciled.state is OperationState.SUCCEEDED
    assert reconciled.deployment_state is DeploymentState.HEALTHY
    assert provider.deploy_calls == [spec.intent.intent_id]


def test_prepare_with_unavailable_node_never_issues_provider_mutation() -> None:
    health = FakeNodeHealth(False)
    coordinator, _, provider, _ = _coordinator(health=health)
    spec = _spec("project-a", "messages")
    coordinator.submit(spec, now=NOW)

    waiting = coordinator.prepare(spec.operation_id, now=NOW)

    assert waiting.state is OperationState.WAITING_FOR_NODE
    assert waiting.node_id is None
    assert provider.deploy_calls == []


def test_snapshot_rejects_serialized_active_node_identity() -> None:
    coordinator, _, _, _ = _coordinator()
    spec = _spec("project-a", "messages")
    bad = DeploymentExecutionSnapshot(
        (
            coordinator.submit(spec, now=NOW).__class__(
                spec,
                OperationState.RECOVERY_REQUIRED,
                node_id="local-linux",
                updated_at=NOW,
            ),
        )
    )

    with pytest.raises(DeploymentExecutionError, match="must not serialize active execution leases"):
        coordinator.restore(bad)


def test_duplicate_operation_snapshot_fails_closed() -> None:
    coordinator, _, _, _ = _coordinator()
    spec = _spec("project-a", "messages")
    record = coordinator.submit(spec, now=NOW)
    snapshot = DeploymentExecutionSnapshot((record, record))

    with pytest.raises(DeploymentExecutionError, match="duplicate operations"):
        coordinator.restore(snapshot)


def test_operation_id_is_idempotent_but_payload_conflicts_fail() -> None:
    coordinator, _, _, _ = _coordinator()
    original = _spec("project-a", "messages")
    coordinator.submit(original, now=NOW)
    assert coordinator.submit(original, now=NOW) == coordinator.get(original.operation_id)

    conflicting = DeploymentExecutionSpec(
        original.operation_id,
        original.request,
        _intent("project-a", "messages", SHA2),
        original.credential_ref,
        original.credential_audience,
        original.credential_scope,
    )
    with pytest.raises(DeploymentExecutionError, match="conflicts with prior"):
        coordinator.submit(conflicting, now=NOW)


def test_project_identity_mismatch_is_rejected() -> None:
    request = ExecutionRequest(
        "project-a",
        "work-a",
        Platform.LINUX,
        frozenset(),
        frozenset(),
        ResourceEnvelope(1, 128, 128),
    )
    with pytest.raises(DeploymentExecutionError, match="project mismatch"):
        DeploymentExecutionSpec(
            "operation-a",
            request,
            _intent("project-b", "messages"),
            "secret:project-a",
            "staging-provider",
            "deploy:staging",
        )


def test_scale_many_services_remain_independent_across_restart() -> None:
    coordinator, _, provider, health = _coordinator()
    specs = [_spec("project-a", f"service-{index:02d}", f"{index + 1:040x}") for index in range(30)]

    for spec in specs[:15]:
        coordinator.submit(spec, now=NOW)
        assert coordinator.prepare(spec.operation_id, now=NOW).state is OperationState.PREPARED
        assert coordinator.complete(spec.operation_id, now=NOW).state is OperationState.SUCCEEDED

    for spec in specs[15:]:
        coordinator.submit(spec, now=NOW)

    snapshot = coordinator.snapshot()
    restored, _, _, _ = _coordinator(provider=provider, health=health)
    restored.restore(snapshot)

    for spec in specs[15:]:
        assert restored.prepare(spec.operation_id, now=NOW).state is OperationState.PREPARED
        assert restored.complete(spec.operation_id, now=NOW).state is OperationState.SUCCEEDED

    assert len(provider.deploy_calls) == 30
    assert len(set(provider.deploy_calls)) == 30
    assert all(restored.get(spec.operation_id).state is OperationState.SUCCEEDED for spec in specs)
