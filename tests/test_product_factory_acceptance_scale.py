from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from nika_core.product_factory_coordinator import (
    CoordinatorError,
    ProductFactoryCoordinator,
    ReviewDecision,
    WorkerResultEnvelope,
    WorkState,
)
from nika_core.product_factory_credentials import (
    CredentialBroker,
    CredentialBrokerError,
    SecretRef,
)
from nika_core.product_factory_deployment import (
    DeploymentFabricError,
    ExecutionNodeRegistry,
    ExecutionRequest,
    Platform,
    ResourceEnvelope,
    local_linux_node,
    local_windows_node,
)
from nika_core.product_factory_orchestration import (
    ComponentBrief,
    DynamicTeamComposer,
    OwnershipLease,
    ProductComponent,
    ProductRepositoryGraph,
    ProjectScale,
    RepositoryRef,
    TeamCompositionRequest,
)
from nika_core.toolsmith.contracts import (
    CodingResult,
    WorkerFailure,
    WorkerFailureKind,
)
from nika_core.toolsmith.contracts import (
    TestEvidence as WorkerTestEvidence,
)

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
DIGEST = "d" * 64
PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})
NOW = datetime(2026, 8, 20, 20, 30, tzinfo=UTC)


def _chain_graph(count: int) -> ProductRepositoryGraph:
    components = []
    for index in range(count):
        component_id = f"component-{index:03d}"
        dependencies = () if index == 0 else (f"component-{index - 1:03d}",)
        components.append(
            ProductComponent(
                component_id=component_id,
                repository_id="repo-main",
                paths=(f"src/{component_id}",),
                dependencies=dependencies,
                test_commands=(
                    ("python", "-m", "pytest", f"tests/{component_id}"),
                ),
            )
        )
    return ProductRepositoryGraph(
        project_id="scale-product",
        repositories=(
            RepositoryRef("repo-main", "github", "owner/scale-product", "main"),
        ),
        components=tuple(components),
    )


def _independent_graph(count: int) -> ProductRepositoryGraph:
    return ProductRepositoryGraph(
        project_id="scale-product",
        repositories=(
            RepositoryRef("repo-main", "github", "owner/scale-product", "main"),
        ),
        components=tuple(
            ProductComponent(
                component_id=f"component-{index:03d}",
                repository_id="repo-main",
                paths=(f"src/component-{index:03d}",),
                test_commands=(
                    ("python", "-m", "pytest", f"tests/component-{index:03d}"),
                ),
            )
            for index in range(count)
        ),
    )


def _plan(graph: ProductRepositoryGraph) -> ProductFactoryCoordinator:
    coordinator = ProductFactoryCoordinator(graph)
    coordinator.plan(
        base_shas={"repo-main": SHA_A},
        goals={component.component_id: component.component_id for component in graph.components},
        permission_ceiling=PERMISSIONS,
    )
    return coordinator


def _success(request) -> WorkerResultEnvelope:
    command = request.acceptance_commands[0]
    return WorkerResultEnvelope(
        request.work_id,
        request.component_id,
        request.repository_id,
        request.base_sha,
        SHA_B,
        DIGEST,
        CodingResult(
            job_id=request.work_id,
            test_evidence=(WorkerTestEvidence(command, 0, f"proof:{request.work_id}"),),
        ),
    )


def _accept(coordinator: ProductFactoryCoordinator, component_id: str, index: int) -> None:
    request = coordinator.start(component_id)
    coordinator.record_result(_success(request))
    coordinator.review(
        component_id,
        ReviewDecision(
            reviewer_id=f"qa-{index:03d}",
            accepted=True,
            reason="independent acceptance evidence verified",
            evidence_refs=(f"qa-evidence:{index:03d}",),
        ),
    )


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
        handle = f"handle-{self.next_handle:04d}"
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


def test_c0_single_component_plan_is_deterministic() -> None:
    graph = _chain_graph(1)
    first = _plan(graph)
    second = _plan(graph)

    assert first.snapshot() == second.snapshot()
    assert len(first.ready_requests()) == 1


def test_c1_eight_component_dependency_chain_executes_in_order() -> None:
    graph = _chain_graph(8)
    coordinator = _plan(graph)

    for index, component_id in enumerate(graph.dependency_order()):
        assert tuple(item.component_id for item in coordinator.ready_requests()) == (
            component_id,
        )
        _accept(coordinator, component_id, index)

    assert all(item.state is WorkState.ACCEPTED for item in coordinator.snapshot().records)


def test_c2_twelve_components_across_four_repositories_keep_dependency_identity() -> None:
    repositories = tuple(
        RepositoryRef(
            f"repo-{index}",
            "github",
            f"owner/product-{index}",
            "main",
        )
        for index in range(4)
    )
    components = []
    for index in range(12):
        component_id = f"service-{index:02d}"
        dependency = () if index == 0 else (f"service-{index - 1:02d}",)
        components.append(
            ProductComponent(
                component_id,
                f"repo-{index % 4}",
                (f"src/{component_id}",),
                dependencies=dependency,
                test_commands=(("python", "-m", "pytest", f"tests/{component_id}"),),
                release_identity=f"{component_id}:v1",
            )
        )
    graph = ProductRepositoryGraph("multi-repo", repositories, tuple(components))

    assert graph.dependency_order() == tuple(
        f"service-{index:02d}" for index in range(12)
    )
    assert {component.repository_id for component in graph.components} == {
        "repo-0",
        "repo-1",
        "repo-2",
        "repo-3",
    }


def test_c3_two_execution_platforms_use_the_same_contract_and_distinct_nodes() -> None:
    registry = ExecutionNodeRegistry()
    registry.register(local_windows_node())
    registry.register(local_linux_node())

    windows = registry.acquire(
        ExecutionRequest(
            "product-a",
            "windows-build",
            Platform.WINDOWS,
            frozenset({"local"}),
            frozenset({"python"}),
            ResourceEnvelope(1, 512, 512),
        ),
        now=NOW,
    )
    linux = registry.acquire(
        ExecutionRequest(
            "product-a",
            "linux-build",
            Platform.LINUX,
            frozenset({"local"}),
            frozenset({"python"}),
            ResourceEnvelope(1, 512, 512),
        ),
        now=NOW,
    )

    assert windows.node_id == "local-windows"
    assert linux.node_id == "local-linux"
    assert windows.node_id != linux.node_id


def test_c3_unavailable_platform_fails_closed_instead_of_faking_success() -> None:
    registry = ExecutionNodeRegistry()
    registry.register(local_windows_node())
    registry.register(local_linux_node())

    with pytest.raises(DeploymentFabricError, match="no available execution node"):
        registry.acquire(
            ExecutionRequest(
                "product-a",
                "mac-build",
                Platform.MACOS,
                frozenset(),
                frozenset(),
                ResourceEnvelope(1, 512, 512),
            ),
            now=NOW,
        )


def test_c4_one_hundred_component_chain_survives_ten_restart_boundaries() -> None:
    graph = _chain_graph(100)
    coordinator = _plan(graph)

    for index, component_id in enumerate(graph.dependency_order(), start=1):
        _accept(coordinator, component_id, index)
        if index % 10 == 0 and index != 100:
            snapshot = coordinator.snapshot()
            restarted = ProductFactoryCoordinator(graph)
            restarted.restore(snapshot)
            coordinator = restarted

    states = {item.state for item in coordinator.snapshot().records}
    assert states == {WorkState.ACCEPTED}
    assert coordinator.ready_requests() == ()


def test_c4_one_blocked_component_does_not_freeze_ninety_nine_independent_owners() -> None:
    coordinator = _plan(_independent_graph(100))
    coordinator.block("component-000", "external dependency unavailable")

    ready = {item.component_id for item in coordinator.ready_requests()}
    assert "component-000" not in ready
    assert len(ready) == 99


def test_c4_large_team_fans_out_and_never_exceeds_permission_ceiling() -> None:
    components = tuple(
        ComponentBrief(
            component_id=f"component-{index:03d}",
            kind=("windows", "backend", "web")[index % 3],
            risk_tags=frozenset({"accessibility"}) if index % 10 == 0 else frozenset(),
        )
        for index in range(100)
    )
    request = TeamCompositionRequest(
        project_id="large-product",
        components=components,
        acceptance_criteria=("Accessible", "tested", "packaged"),
        permission_ceiling=frozenset({"read_source", "run_tests"}),
        scale=ProjectScale.LARGE,
        evidence_refs=("evidence:requirements",),
    )
    composer = DynamicTeamComposer()

    first = composer.compose(request)
    second = composer.compose(request)
    implementation = [
        role for role in first.roles if role.capabilities == ("implementation",)
    ]

    assert first == second
    assert len(implementation) == 100
    assert all(role.permissions <= request.permission_ceiling for role in first.roles)
    assert all("write_source" not in role.permissions for role in first.roles)


def test_c4_fifty_parallel_nonoverlapping_leases_have_no_false_conflicts() -> None:
    graph = _independent_graph(50)
    active = tuple(
        OwnershipLease(
            f"lease-{index:03d}",
            f"worker-{index:03d}",
            (f"component-{index:03d}",),
            (f"src/component-{index:03d}",),
        )
        for index in range(49)
    )
    candidate = OwnershipLease(
        "lease-049",
        "worker-049",
        ("component-049",),
        ("src/component-049",),
    )

    assessment = graph.assess_lease(candidate, active)
    assert assessment.grantable is True
    assert assessment.conflicts == ()


def test_c5_review_required_state_survives_twenty_restart_cycles_without_promotion() -> None:
    graph = _chain_graph(1)
    coordinator = _plan(graph)
    request = coordinator.start("component-000")
    coordinator.record_result(_success(request))

    for _ in range(20):
        snapshot = coordinator.snapshot()
        restarted = ProductFactoryCoordinator(graph)
        restarted.restore(snapshot)
        coordinator = restarted

    record = coordinator.snapshot().records[0]
    assert record.state is WorkState.REVIEW_REQUIRED
    assert record.review is None
    assert coordinator.ready_requests() == ()


def test_c5_superseded_attempt_rejects_late_result_from_prior_worker_cycle() -> None:
    graph = _chain_graph(1)
    coordinator = _plan(graph)
    first = coordinator.start("component-000")
    failure = WorkerResultEnvelope(
        first.work_id,
        first.component_id,
        first.repository_id,
        first.base_sha,
        SHA_B,
        DIGEST,
        CodingResult(
            job_id=first.work_id,
            failure=WorkerFailure(
                WorkerFailureKind.PROCESS_FAILED,
                "candidate failed acceptance",
                retryable=True,
            ),
        ),
    )
    coordinator.record_result(failure)
    repair = coordinator.prepare_repair(
        "component-000",
        base_sha=SHA_C,
        reason="repair failed acceptance",
    )
    coordinator.start("component-000")

    assert repair.attempt == 2
    with pytest.raises(CoordinatorError, match="identity"):
        coordinator.record_result(_success(first))


def test_pf7_twenty_five_projects_cannot_enumerate_each_others_secret_refs() -> None:
    store = _ProtectedStore()
    broker = CredentialBroker(store)
    for index in range(25):
        project_id = f"project-{index:02d}"
        secret_ref = f"secret-{index:02d}"
        store.material.add((secret_ref, 1))
        broker.register_secret(
            SecretRef(
                secret_ref,
                project_id,
                "github",
                "repository read",
                frozenset({"repo:read"}),
                frozenset({"github-api"}),
            ),
            now=NOW,
        )

    for index in range(25):
        project_id = f"project-{index:02d}"
        secret_ref = f"secret-{index:02d}"
        visible = broker.list_project_secret_refs(project_id)
        assert tuple(item.secret_ref for item in visible) == (secret_ref,)
        foreign_ref = f"secret-{(index + 1) % 25:02d}"
        with pytest.raises(CredentialBrokerError, match="unavailable for project"):
            broker.issue_lease(
                project_id=project_id,
                secret_ref=foreign_ref,
                audience="github-api",
                scopes=frozenset({"repo:read"}),
                now=NOW,
            )
