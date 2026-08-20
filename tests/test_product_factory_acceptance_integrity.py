from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_credentials import (
    CredentialBroker,
    CredentialBrokerError,
    CredentialBrokerSnapshot,
    SecretRef,
)
from nika_core.product_factory_deployment import (
    DeploymentFabric,
    DeploymentFabricError,
    DeploymentFabricSnapshot,
    DeploymentIntent,
    EnvironmentIdentity,
    EnvironmentTier,
    ExecutionNodeRegistry,
    ExecutionRegistrySnapshot,
    HealthEvidence,
    ProviderDeploymentResult,
    ProviderInspection,
    ReleaseRef,
    RollbackEvidence,
    WorkLease,
    local_windows_node,
)
from nika_core.product_factory_orchestration import (
    ComponentBrief,
    DynamicTeamComposer,
    ProductComponent,
    ProductRepositoryGraph,
    ProjectScale,
    RepositoryGraphError,
    RepositoryRef,
    TeamCompositionError,
    TeamCompositionRequest,
)
from nika_core.product_project import (
    EvidenceRef,
    ProductOption,
    ProductProjectError,
    ProductProjectRepository,
    ProductProjectSpec,
    ProductRequirement,
    ResearchEvidencePackage,
)

NOW = datetime(2026, 8, 20, 21, 0, tzinfo=UTC)
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
DIGEST_A = "1" * 64
DIGEST_B = "2" * 64


@dataclass(slots=True)
class _ProtectedStore:
    material: set[tuple[str, int]] = field(default_factory=set)
    handles: dict[str, tuple[str, int]] = field(default_factory=dict)
    next_handle: int = 1

    def contains(self, secret_ref: str, generation: int) -> bool:
        return (secret_ref, generation) in self.material

    def issue_handle(
        self,
        *,
        secret_ref: str,
        generation: int,
        project_id: str,
        audience: str,
        scopes: frozenset[str],
        expires_at: datetime,
    ) -> str:
        assert project_id and audience and scopes and expires_at > NOW
        handle = f"protected-handle-{self.next_handle:04d}"
        self.next_handle += 1
        self.handles[handle] = (secret_ref, generation)
        return handle

    def revoke_handles(self, secret_ref: str, generation: int) -> None:
        for handle in [
            key
            for key, value in self.handles.items()
            if value == (secret_ref, generation)
        ]:
            del self.handles[handle]


class _RollbackMismatchProvider:
    def __init__(self) -> None:
        self.health_calls = 0

    def deploy(self, intent: DeploymentIntent) -> ProviderDeploymentResult:
        return ProviderDeploymentResult(True, False, (f"deploy:{intent.intent_id}",))

    def health(self, intent: DeploymentIntent) -> HealthEvidence:
        self.health_calls += 1
        healthy = self.health_calls == 1
        return HealthEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            healthy,
            (f"health:{intent.intent_id}",),
            NOW,
        )

    def rollback(
        self,
        intent: DeploymentIntent,
        previous_release_sha: str | None,
    ) -> RollbackEvidence:
        assert previous_release_sha == SHA_A
        return RollbackEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            SHA_C,
            True,
            (f"rollback:{intent.intent_id}",),
        )

    def inspect(self, intent: DeploymentIntent) -> ProviderInspection:
        return ProviderInspection(
            intent.release.source_sha,
            True,
            (f"inspect:{intent.intent_id}",),
        )


def _project_repo(tmp_path) -> ProductProjectRepository:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repo = ProductProjectRepository(store)
    repo.create(
        project_id="p1",
        name="Product",
        spec=ProductProjectSpec(
            goal="Build product",
            desired_outcome="Reviewed product",
        ),
        idempotency_key="create:p1",
    )
    return repo


def _intent(intent_id: str, sha: str, digest: str) -> DeploymentIntent:
    return DeploymentIntent(
        intent_id,
        "p1",
        EnvironmentIdentity("staging", "p1", EnvironmentTier.STAGING, "provider:p1"),
        ReleaseRef("p1", intent_id, sha, digest),
    )


def test_pf1_research_handoff_rejects_phantom_evidence_package_reference(tmp_path) -> None:
    repo = _project_repo(tmp_path)
    package = ResearchEvidencePackage(
        "package-real",
        (EvidenceRef("evidence-1", "research://claim/1", "claim"),),
    )
    option = ProductOption(
        "option-1",
        "Option",
        "Summary",
        ("package-real", "package-never-recorded"),
    )

    with pytest.raises(ProductProjectError):
        repo.record_research_handoff("p1", package, (option,))


def test_pf1_requirement_identity_must_be_unique_inside_a_product_spec() -> None:
    first = ProductRequirement("req-1", "Keyboard", ("keyboard works",))
    duplicate = ProductRequirement("req-1", "Screen reader", ("NVDA works",))

    with pytest.raises(ProductProjectError):
        ProductProjectSpec(
            goal="Build product",
            desired_outcome="Unambiguous requirements",
            requirements=(first, duplicate),
        )


def test_pf2_dynamic_specialist_cannot_own_a_phantom_component() -> None:
    composer = DynamicTeamComposer()
    plan = composer.compose(
        TeamCompositionRequest(
            project_id="p1",
            components=(ComponentBrief("core", "backend"),),
            acceptance_criteria=("tested",),
            permission_ceiling=frozenset({"read_source", "run_tests"}),
            scale=ProjectScale.SMALL,
        )
    )

    with pytest.raises(TeamCompositionError):
        composer.add_specialist(
            plan,
            specialization="security",
            component_ids=("component-does-not-exist",),
            requested_permissions=("read_source",),
            reason="security review",
        )


def test_pf3_repository_credential_reference_requires_a_nonempty_opaque_identity() -> None:
    with pytest.raises(RepositoryGraphError):
        RepositoryRef(
            "repo",
            "github",
            "owner/repo",
            "main",
            credential_ref="credref:",
        )


def test_pf3_component_identity_cannot_be_empty() -> None:
    with pytest.raises(RepositoryGraphError):
        ProductRepositoryGraph(
            project_id="p1",
            repositories=(RepositoryRef("repo", "github", "owner/repo", "main"),),
            components=(ProductComponent("", "repo", ("src/core",)),),
        )


def test_pf5_execution_restore_rejects_two_active_leases_on_one_node() -> None:
    node = local_windows_node()
    snapshot = ExecutionRegistrySnapshot(
        (node,),
        (
            WorkLease(
                "lease-1",
                "project-a",
                "work-a",
                node.identity.node_id,
                NOW,
                NOW + timedelta(minutes=5),
            ),
            WorkLease(
                "lease-2",
                "project-b",
                "work-b",
                node.identity.node_id,
                NOW,
                NOW + timedelta(minutes=5),
            ),
        ),
        3,
    )

    with pytest.raises(DeploymentFabricError):
        ExecutionNodeRegistry().restore(snapshot)


def test_pf5_execution_restore_rejects_invalid_lease_time_order() -> None:
    node = local_windows_node()
    snapshot = ExecutionRegistrySnapshot(
        (node,),
        (
            WorkLease(
                "lease-1",
                "project-a",
                "work-a",
                node.identity.node_id,
                NOW,
                NOW - timedelta(seconds=1),
            ),
        ),
        2,
    )

    with pytest.raises(DeploymentFabricError):
        ExecutionNodeRegistry().restore(snapshot)


def test_pf6_environment_identity_requires_environment_and_provider_identity() -> None:
    with pytest.raises(DeploymentFabricError):
        EnvironmentIdentity("", "p1", EnvironmentTier.STAGING, "")


def test_pf6_rollback_success_must_restore_the_recorded_previous_release() -> None:
    provider = _RollbackMismatchProvider()
    fabric = DeploymentFabric(provider)
    first = fabric.deploy(_intent("v1", SHA_A, DIGEST_A))

    assert first.state.value == "healthy"
    with pytest.raises(DeploymentFabricError):
        fabric.deploy(_intent("v2", SHA_B, DIGEST_B))


def test_pf6_restore_rejects_current_release_not_backed_by_snapshot_record() -> None:
    provider = _RollbackMismatchProvider()
    fabric = DeploymentFabric(provider)
    fabric.deploy(_intent("v1", SHA_A, DIGEST_A))
    snapshot = fabric.snapshot()
    corrupt = DeploymentFabricSnapshot(
        snapshot.records,
        snapshot.healthy_staging,
        (("staging", SHA_C),),
    )

    with pytest.raises(DeploymentFabricError):
        DeploymentFabric(provider).restore(corrupt)


def test_pf7_restart_invalidates_protected_handles_from_pre_restart_leases() -> None:
    store = _ProtectedStore()
    store.material.add(("secret-a", 1))
    broker = CredentialBroker(store)
    broker.register_secret(
        SecretRef(
            "secret-a",
            "project-a",
            "github",
            "repository read",
            frozenset({"repo:read"}),
            frozenset({"github-api"}),
        ),
        now=NOW,
    )
    lease = broker.issue_lease(
        project_id="project-a",
        secret_ref="secret-a",
        audience="github-api",
        scopes=frozenset({"repo:read"}),
        now=NOW,
    )
    snapshot = broker.snapshot()

    assert lease.handle_ref in store.handles
    CredentialBroker(store).restore(snapshot)
    assert lease.handle_ref not in store.handles


def test_pf7_restore_rejects_audit_counter_rollback() -> None:
    store = _ProtectedStore()
    store.material.add(("secret-a", 1))
    broker = CredentialBroker(store)
    broker.register_secret(
        SecretRef(
            "secret-a",
            "project-a",
            "github",
            "repository read",
            frozenset({"repo:read"}),
            frozenset({"github-api"}),
        ),
        now=NOW,
    )
    snapshot = broker.snapshot()
    rolled_back_counter = CredentialBrokerSnapshot(
        snapshot.secrets,
        snapshot.identities,
        snapshot.audit_events,
        snapshot.next_lease,
        1,
    )

    with pytest.raises(CredentialBrokerError):
        CredentialBroker(store).restore(rolled_back_counter)
