"""AUD02 QA_ONLY oracle for same-project PF7 credential-provider substitution."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from nika_core.product_factory_credentials import CredentialBroker, SecretRef
from nika_core.product_factory_deployment import (
    DeploymentFabric,
    DeploymentIntent,
    EnvironmentIdentity,
    EnvironmentTier,
    ExecutionNodeRegistry,
    ExecutionRequest,
    Platform,
    ReleaseRef,
    ResourceEnvelope,
    local_linux_node,
)
from nika_core.product_factory_deployment_execution import (
    DeploymentExecutionCoordinator,
    DeploymentExecutionSpec,
    OperationState,
)

NOW = datetime(2026, 8, 24, 7, 30, tzinfo=UTC)
SHA = "1" * 40
DIGEST = "a" * 64


@dataclass(slots=True)
class _ProtectedStore:
    material: set[tuple[str, int]] = field(default_factory=set)
    authorities: dict[tuple[str, int], str] = field(default_factory=dict)

    def contains(self, secret_ref: str, generation: int) -> bool:
        return (secret_ref, generation) in self.material

    def bind_authority(
        self,
        *,
        secret_ref: str,
        generation: int,
        authority_fingerprint: str,
    ) -> None:
        self.authorities[(secret_ref, generation)] = authority_fingerprint

    def authority_matches(
        self,
        *,
        secret_ref: str,
        generation: int,
        authority_fingerprint: str,
    ) -> bool:
        return self.authorities.get((secret_ref, generation)) == authority_fingerprint

    def retire_authority(self, **kwargs: object) -> None:
        del kwargs
        raise AssertionError("not used")

    def issue_handle(self, **kwargs: object) -> str:
        return f"opaque:{kwargs['operation_id']}"

    def reconcile_handle(self, **kwargs: object) -> str | None:
        del kwargs
        return None

    def revoke_handles(self, secret_ref: str, generation: int) -> None:
        del secret_ref, generation


class _NodeHealth:
    def is_available(self, node_id: str) -> bool:
        return bool(node_id)


class _Provider:
    """Deployment provider is never reached by this prepare-only authority attack."""


def test_same_project_secret_cannot_be_substituted_for_another_provider() -> None:
    store = _ProtectedStore({("secret-provider-a", 1)})
    credentials = CredentialBroker(store)
    credentials.register_secret(
        SecretRef(
            "secret-provider-a",
            "project-a",
            "provider-a",
            "deployment for provider A",
            frozenset({"deploy:staging"}),
            frozenset({"shared-deployment-api"}),
        ),
        now=NOW,
    )

    nodes = ExecutionNodeRegistry()
    nodes.register(local_linux_node())
    coordinator = DeploymentExecutionCoordinator(
        nodes,
        credentials,
        DeploymentFabric(_Provider()),  # type: ignore[arg-type]
        _NodeHealth(),
    )
    intent = DeploymentIntent(
        "deploy-provider-b",
        "project-a",
        EnvironmentIdentity(
            "staging-provider-b",
            "project-a",
            EnvironmentTier.STAGING,
            "provider-b",
        ),
        ReleaseRef("project-a", "1.0.0", SHA, DIGEST),
    )
    spec = DeploymentExecutionSpec(
        "operation-provider-b",
        ExecutionRequest(
            "project-a",
            "work-provider-b",
            Platform.LINUX,
            frozenset(),
            frozenset(),
            ResourceEnvelope(1, 128, 128),
        ),
        intent,
        "secret-provider-a",
        "shared-deployment-api",
        "deploy:staging",
    )

    coordinator.submit(spec, now=NOW)
    prepared = coordinator.prepare(spec.operation_id, now=NOW)

    assert prepared.state is OperationState.BLOCKED_CREDENTIAL
    assert not any(event.action == "lease" for event in credentials.audit_events("project-a"))
