from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_coordinator import (
    CoordinatorError,
    CoordinatorSnapshot,
    ProductFactoryCoordinator,
    WorkerResultEnvelope,
    WorkRecord,
    WorkState,
)
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
from nika_core.product_factory_orchestration import (
    IntegrationDecision,
    IntegrationDecisionKind,
    OwnershipLease,
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryGraphError,
    RepositoryRef,
)
from nika_core.product_factory_project_binding import (
    ProductProjectCoordinatorBinding,
    StaleProductProjectBindingError,
)
from nika_core.product_project import (
    ProductProjectError,
    ProductProjectRepository,
    ProductProjectSpec,
    ProductRequirement,
)
from nika_core.toolsmith.contracts import CodingResult
from nika_core.toolsmith.contracts import (
    TestEvidence as WorkerTestEvidence,
)

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
DIGEST_A = "1" * 64
DIGEST_B = "2" * 64
PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})
NOW = datetime(2026, 8, 20, 20, 0, tzinfo=UTC)


def _spec(goal: str = "Build an accessible product") -> ProductProjectSpec:
    return ProductProjectSpec(
        goal=goal,
        desired_outcome="A reviewed and restart-safe product",
        requirements=(
            ProductRequirement(
                "req-keyboard",
                "Keyboard operation",
                ("All primary actions are keyboard reachable",),
            ),
        ),
        repository_refs=("owner/product",),
    )


def _project(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    projects = ProductProjectRepository(store)
    project = projects.create(
        project_id="product-1",
        name="Product",
        spec=_spec(),
        idempotency_key="create:product-1",
    )
    return store, projects, project


def _dependency_graph() -> ProductRepositoryGraph:
    return ProductRepositoryGraph(
        project_id="product-1",
        repositories=(
            RepositoryRef("repo-main", "github", "owner/product", "main"),
        ),
        components=(
            ProductComponent(
                component_id="core",
                repository_id="repo-main",
                paths=("src/core",),
                test_commands=(
                    ("python", "-m", "pytest", "tests/core"),
                    ("python", "-m", "pytest", "tests/integration"),
                ),
            ),
            ProductComponent(
                component_id="ui",
                repository_id="repo-main",
                paths=("src/ui",),
                dependencies=("core",),
                test_commands=(("python", "-m", "pytest", "tests/ui"),),
            ),
        ),
    )


def _planned(graph: ProductRepositoryGraph) -> ProductFactoryCoordinator:
    coordinator = ProductFactoryCoordinator(graph)
    coordinator.plan(
        base_shas={repository.repository_id: SHA_A for repository in graph.repositories},
        goals={
            component.component_id: f"Implement {component.component_id}"
            for component in graph.components
        },
        permission_ceiling=PERMISSIONS,
    )
    return coordinator


def _successful_envelope(request, *, commands=None) -> WorkerResultEnvelope:
    evidence_commands = commands or request.acceptance_commands
    return WorkerResultEnvelope(
        work_id=request.work_id,
        component_id=request.component_id,
        repository_id=request.repository_id,
        base_sha=request.base_sha,
        result_sha=SHA_B,
        diff_digest=DIGEST_A,
        coding_result=CodingResult(
            job_id=request.work_id,
            test_evidence=tuple(
                WorkerTestEvidence(command, 0, f"evidence-{index}")
                for index, command in enumerate(evidence_commands, start=1)
            ),
        ),
    )


class _DeploymentProvider:
    def __init__(self, *, unhealthy_shas: frozenset[str] = frozenset()) -> None:
        self.unhealthy_shas = unhealthy_shas
        self.rollback_previous: list[str | None] = []

    def deploy(self, intent: DeploymentIntent) -> ProviderDeploymentResult:
        return ProviderDeploymentResult(True, False, (f"deploy:{intent.intent_id}",))

    def health(self, intent: DeploymentIntent) -> HealthEvidence:
        return HealthEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            intent.release.source_sha not in self.unhealthy_shas,
            (f"health:{intent.intent_id}",),
            NOW,
        )

    def rollback(
        self,
        intent: DeploymentIntent,
        previous_release_sha: str | None,
    ) -> RollbackEvidence:
        self.rollback_previous.append(previous_release_sha)
        return RollbackEvidence(
            intent.environment.environment_id,
            intent.release.source_sha,
            previous_release_sha,
            True,
            (f"rollback:{intent.intent_id}",),
        )

    def inspect(self, intent: DeploymentIntent) -> ProviderInspection:
        return ProviderInspection(
            intent.release.source_sha,
            True,
            (f"inspect:{intent.intent_id}",),
        )


def _staging_intent(
    project_id: str,
    intent_id: str,
    source_sha: str,
    digest: str,
    *,
    environment_id: str = "shared-staging",
) -> DeploymentIntent:
    return DeploymentIntent(
        intent_id,
        project_id,
        EnvironmentIdentity(
            environment_id,
            project_id,
            EnvironmentTier.STAGING,
            f"provider-ref:{project_id}",
        ),
        ReleaseRef(project_id, "1.0.0", source_sha, digest),
    )


def test_pf12_durable_mutation_invalidates_checkpoint_after_required_rebind(tmp_path) -> None:
    """Recovery must bind durable checkpoint state to the current ProductProject version."""
    _, projects, project = _project(tmp_path)
    graph = _dependency_graph()
    binding = ProductProjectCoordinatorBinding(project, graph)
    coordinator = _planned(graph)
    checkpoint = binding.checkpoint(coordinator)

    projects.update_spec(
        "product-1",
        _spec("Build a materially changed accessible product"),
        expected_row_version=project.row_version,
    )
    current_project = projects.get("product-1")
    restarted_binding = ProductProjectCoordinatorBinding(current_project, graph)

    with pytest.raises(StaleProductProjectBindingError):
        restarted_binding.restore(checkpoint)


def test_pf12_restore_rejects_forged_accepted_state_without_result_or_review() -> None:
    """Restart may not manufacture an accepted dependency from serialized state alone."""
    graph = _dependency_graph()
    coordinator = _planned(graph)
    snapshot = coordinator.snapshot()
    records = {item.request.component_id: item for item in snapshot.records}
    forged_core = WorkRecord(records["core"].request, WorkState.ACCEPTED)
    forged = CoordinatorSnapshot(
        snapshot.project_id,
        snapshot.revision + 1,
        (forged_core, records["ui"]),
    )

    with pytest.raises(CoordinatorError):
        ProductFactoryCoordinator(graph).restore(forged)


def test_pf12_restore_rejects_request_repository_identity_drift() -> None:
    graph = _dependency_graph()
    coordinator = _planned(graph)
    snapshot = coordinator.snapshot()
    records = {item.request.component_id: item for item in snapshot.records}
    core = records["core"]
    wrong_request = replace(core.request, repository_id="repo-not-in-graph")
    forged = CoordinatorSnapshot(
        snapshot.project_id,
        snapshot.revision + 1,
        (WorkRecord(wrong_request, core.state), records["ui"]),
    )

    with pytest.raises(CoordinatorError):
        ProductFactoryCoordinator(graph).restore(forged)


def test_pf12_restore_rejects_request_project_identity_drift() -> None:
    graph = _dependency_graph()
    coordinator = _planned(graph)
    snapshot = coordinator.snapshot()
    records = {item.request.component_id: item for item in snapshot.records}
    core = records["core"]
    wrong_request = replace(core.request, project_id="other-product")
    forged = CoordinatorSnapshot(
        snapshot.project_id,
        snapshot.revision + 1,
        (WorkRecord(wrong_request, core.state), records["ui"]),
    )

    with pytest.raises(CoordinatorError):
        ProductFactoryCoordinator(graph).restore(forged)


def test_pf12_restore_rejects_scope_or_permission_expansion() -> None:
    graph = _dependency_graph()
    coordinator = _planned(graph)
    snapshot = coordinator.snapshot()
    records = {item.request.component_id: item for item in snapshot.records}
    core = records["core"]
    expanded = replace(
        core.request,
        allowed_paths=("src",),
        permission_ceiling=core.request.permission_ceiling | {"admin_project"},
    )
    forged = CoordinatorSnapshot(
        snapshot.project_id,
        snapshot.revision + 1,
        (WorkRecord(expanded, core.state), records["ui"]),
    )

    with pytest.raises(CoordinatorError):
        ProductFactoryCoordinator(graph).restore(forged)


def test_pf4_success_requires_every_declared_acceptance_command() -> None:
    """One easy green test cannot stand in for the component acceptance matrix."""
    coordinator = _planned(_dependency_graph())
    request = coordinator.start("core")
    only_first_command = (request.acceptance_commands[0],)

    with pytest.raises(CoordinatorError):
        coordinator.record_result(
            _successful_envelope(request, commands=only_first_command)
        )


def test_pf4_unrelated_passing_command_cannot_satisfy_acceptance() -> None:
    coordinator = _planned(_dependency_graph())
    request = coordinator.start("core")
    unrelated = (("python", "-c", "print('green')"),)

    with pytest.raises(CoordinatorError):
        coordinator.record_result(_successful_envelope(request, commands=unrelated))


def test_pf3_same_physical_repository_cannot_hide_behind_two_repository_ids() -> None:
    """Repository aliases must not bypass overlap and ownership checks."""
    with pytest.raises(RepositoryGraphError):
        ProductRepositoryGraph(
            project_id="product-1",
            repositories=(
                RepositoryRef("repo-a", "github", "owner/same-repo", "main"),
                RepositoryRef("repo-b", "github", "owner/same-repo", "main"),
            ),
            components=(
                ProductComponent("api", "repo-a", ("src/api",)),
                ProductComponent("worker", "repo-b", ("src/api/worker",)),
            ),
        )


def test_pf3_integration_decision_must_name_the_actual_conflicting_active_lease() -> None:
    graph = ProductRepositoryGraph(
        project_id="product-1",
        repositories=(RepositoryRef("repo", "github", "owner/repo", "main"),),
        components=(ProductComponent("api", "repo", ("src/api",)),),
    )
    active = OwnershipLease(
        "lease-active",
        "worker-active",
        ("api",),
        ("src/api",),
    )
    candidate = OwnershipLease(
        "lease-candidate",
        "worker-candidate",
        ("api",),
        ("src/api/routes",),
    )
    malformed = IntegrationDecision(
        "decision-malformed",
        IntegrationDecisionKind.RECONCILE,
        ("lease-candidate", "lease-candidate"),
        "claim reconciliation without naming the conflicting owner",
        ("evidence:compare",),
    )

    with pytest.raises(RepositoryGraphError):
        graph.assess_lease(candidate, (active,), decision=malformed)


def test_pf6_current_release_identity_is_scoped_by_project_and_environment() -> None:
    """Two products may both have an environment called shared-staging."""
    provider = _DeploymentProvider()
    fabric = DeploymentFabric(provider)
    first = fabric.deploy(
        _staging_intent("product-a", "deploy-a", SHA_A, DIGEST_A)
    )
    second = fabric.deploy(
        _staging_intent("product-b", "deploy-b", SHA_B, DIGEST_B)
    )

    assert first.previous_release_sha is None
    assert second.previous_release_sha is None
    assert len(fabric.snapshot().current_releases) == 2


def test_pf6_rollback_never_uses_another_projects_release_as_previous() -> None:
    provider = _DeploymentProvider(unhealthy_shas=frozenset({SHA_B}))
    fabric = DeploymentFabric(provider)
    fabric.deploy(_staging_intent("product-a", "deploy-a", SHA_A, DIGEST_A))
    rejected = fabric.deploy(
        _staging_intent("product-b", "deploy-b", SHA_B, DIGEST_B)
    )

    assert rejected.state is DeploymentState.ROLLED_BACK
    assert rejected.previous_release_sha is None
    assert provider.rollback_previous == [None]


def test_pf7_raw_secret_shaped_scalar_cannot_hide_under_an_innocent_key() -> None:
    with pytest.raises(ProductProjectError):
        ProductProjectSpec(
            goal="Build a product",
            desired_outcome="No secrets in durable project state",
            compliance={
                "notes": [
                    "ordinary note",
                    "ghp_0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcd",
                ]
            },
        )


def test_pf7_raw_secret_cannot_be_mislabelled_as_a_credential_reference() -> None:
    with pytest.raises(ProductProjectError):
        ProductProjectSpec(
            goal="Build a product",
            desired_outcome="Opaque credentials only",
            credential_refs=("ghp_0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcd",),
        )
