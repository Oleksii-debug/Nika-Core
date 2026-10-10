from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_checkpoint_host import ProductFactoryCheckpointHost
from nika_core.product_factory_coordinator import (
    ProductFactoryCoordinator,
    ReviewDecision,
    WorkerResultEnvelope,
    WorkState,
)
from nika_core.product_factory_incident_contracts import (
    IncidentKind,
    IncidentSeverity,
    IncidentState,
    IncidentTrigger,
)
from nika_core.product_factory_incidents import IncidentRepairReleaseCoordinator
from nika_core.product_factory_operations import ProductOperationsCoordinator
from nika_core.product_factory_operations_contracts import (
    DeployableService,
    ServiceHealth,
    ServiceObservation,
    ServiceReplica,
)
from nika_core.product_factory_orchestration import (
    ComponentBrief,
    DynamicTeamComposer,
    IntegrationDecision,
    IntegrationDecisionKind,
    OwnershipLease,
    ProductComponent,
    ProductRepositoryGraph,
    ProjectScale,
    RepositoryGraphError,
    RepositoryRef,
    TeamCompositionRequest,
)
from nika_core.product_factory_project_binding import ProductProjectCoordinatorBinding
from nika_core.product_project import (
    ProductProjectRepository,
    ProductProjectSpec,
    ProductRequirement,
)
from nika_core.toolsmith.contracts import (
    CodingResult,
    TestEvidence,
    WorkerFailure,
    WorkerFailureKind,
)

PROJECT_ID = "c4-social-scale"
PERMISSIONS = frozenset({"read_source", "write_source", "run_tests", "build_release"})
STAGES = ("contract", "storage", "service", "integration", "acceptance")
DOMAIN_SPECS = (
    ("identity", "platform", "backend", frozenset({"privacy", "security"}), ()),
    ("profiles", "platform", "backend", frozenset({"privacy"}), ("identity",)),
    ("graph", "social", "data", frozenset({"privacy"}), ("profiles",)),
    ("posts", "content", "backend", frozenset(), ("identity", "profiles")),
    ("media", "content", "data", frozenset({"privacy"}), ("posts",)),
    ("moderation", "trust", "backend", frozenset({"security"}), ("posts", "media")),
    ("feed", "social", "backend", frozenset(), ("graph", "posts", "moderation")),
    ("notifications", "messaging", "backend", frozenset(), ("identity", "feed")),
    ("search", "search", "data", frozenset(), ("profiles", "posts")),
    ("settings", "platform", "backend", frozenset({"privacy"}), ("identity", "profiles")),
    (
        "desktop",
        "client",
        "desktop",
        frozenset({"accessibility"}),
        ("feed", "notifications", "search", "settings"),
    ),
    ("operations", "ops", "infra", frozenset({"deployment"}), ("desktop",)),
)
REPOSITORIES = (
    "platform",
    "social",
    "content",
    "trust",
    "messaging",
    "search",
    "client",
    "ops",
)


def _sha(index: int) -> str:
    return f"{index:040x}"[-40:]


def _digest(index: int) -> str:
    return f"{index:064x}"[-64:]


def _component_id(domain: str, stage: str) -> str:
    return f"{domain}-{stage}"


def _social_graph() -> ProductRepositoryGraph:
    repositories = tuple(
        RepositoryRef(
            repository_id=f"repo-{name}",
            provider="github",
            locator=f"example/c4-{name}",
            default_branch="main",
        )
        for name in REPOSITORIES
    )
    components: list[ProductComponent] = []
    for domain, repository, _kind, _risks, cross_domains in DOMAIN_SPECS:
        for index, stage in enumerate(STAGES):
            dependencies: list[str] = []
            if index:
                dependencies.append(_component_id(domain, STAGES[index - 1]))
            if stage == "integration":
                dependencies.extend(
                    _component_id(cross_domain, "service")
                    for cross_domain in cross_domains
                )
            components.append(
                ProductComponent(
                    component_id=_component_id(domain, stage),
                    repository_id=f"repo-{repository}",
                    paths=(f"domains/{domain}/{stage}",),
                    dependencies=tuple(dependencies),
                    build_commands=(
                        ("python", "-m", "compileall", f"src/{domain}/{stage}"),
                    ),
                    test_commands=(
                        ("python", "-m", "pytest", f"tests/{domain}/{stage}"),
                    ),
                    release_identity=f"{domain}-{stage}:1.0.0",
                )
            )
    return ProductRepositoryGraph(
        project_id=PROJECT_ID,
        repositories=repositories,
        components=tuple(components),
    )


def _initial_spec(graph: ProductRepositoryGraph) -> ProductProjectSpec:
    requirements = tuple(
        ProductRequirement(
            requirement_id=f"req-{domain}",
            text=f"{domain} capability must remain independently testable and releasable",
            acceptance=(f"{domain} acceptance component reaches independent review",),
        )
        for domain, _repository, _kind, _risks, _cross in DOMAIN_SPECS
    )
    return ProductProjectSpec(
        goal="Exercise a deterministic social-network-scale ProductProject graph",
        desired_outcome="Prove factory scale, restart, repair, release planning and operations",
        requirements=requirements,
        repository_refs=tuple(repository.locator for repository in graph.repositories),
    )


def _setup(tmp_path):
    graph = _social_graph()
    store = SQLiteStore(tmp_path / "nika-c4-social.db")
    store.initialize()
    projects = ProductProjectRepository(store)
    project = projects.create(
        project_id=PROJECT_ID,
        name="C4 Social Scale Acceptance",
        spec=_initial_spec(graph),
        idempotency_key="create:c4-social-scale",
    )
    revised_spec = replace(
        project.spec,
        build_refs=("build-plan:c4-social:v2",),
        release_refs=("release-plan:c4-social:v2",),
    )
    project = projects.update_spec(
        PROJECT_ID,
        revised_spec,
        expected_row_version=project.row_version,
        change_reason="bind deterministic C4 build and release planning",
    )
    binding = ProductProjectCoordinatorBinding(project, graph)
    task = TaskQueue(store).create(
        workspace_id="ws-c4-social",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": PROJECT_ID},
    )
    coordinator = binding.plan(
        base_shas={
            repository.repository_id: _sha(index + 1)
            for index, repository in enumerate(graph.repositories)
        },
        component_goals={
            component.component_id: f"Implement bounded {component.component_id}"
            for component in graph.components
        },
        permission_ceiling=PERMISSIONS,
    )
    return store, projects, binding, task.task_id, graph, coordinator


def _successful_envelope(request, ordinal: int) -> WorkerResultEnvelope:
    return WorkerResultEnvelope(
        work_id=request.work_id,
        component_id=request.component_id,
        repository_id=request.repository_id,
        base_sha=request.base_sha,
        result_sha=_sha(100_000 + ordinal),
        diff_digest=_digest(200_000 + ordinal),
        coding_result=CodingResult(
            job_id=request.work_id,
            test_evidence=(
                TestEvidence(
                    command=request.acceptance_commands[0],
                    exit_code=0,
                    output_digest=_digest(300_000 + ordinal),
                ),
            ),
        ),
    )


def _accept(coordinator: ProductFactoryCoordinator, component_id: str, ordinal: int) -> None:
    request = coordinator.start(component_id)
    coordinator.record_result(_successful_envelope(request, ordinal))
    reviewed = coordinator.review(
        component_id,
        ReviewDecision(
            reviewer_id=f"qa:{component_id}",
            accepted=True,
            reason="independent C4 acceptance passed",
            evidence_refs=(f"review:{component_id}",),
        ),
    )
    assert reviewed.state is WorkState.ACCEPTED


def _record(coordinator: ProductFactoryCoordinator, component_id: str):
    return next(
        record
        for record in coordinator.snapshot().records
        if record.request.component_id == component_id
    )


def _team_plan(graph: ProductRepositoryGraph):
    kind_by_domain = {
        domain: kind for domain, _repo, kind, _risks, _cross in DOMAIN_SPECS
    }
    risks_by_domain = {
        domain: risks for domain, _repo, _kind, risks, _cross in DOMAIN_SPECS
    }
    request = TeamCompositionRequest(
        project_id=PROJECT_ID,
        components=tuple(
            ComponentBrief(
                component.component_id,
                kind_by_domain[component.component_id.split("-", 1)[0]],
                risks_by_domain[component.component_id.split("-", 1)[0]],
            )
            for component in graph.components
        ),
        acceptance_criteria=(
            "All components receive independent QA",
            "Desktop surface remains accessible",
            "Build, release, rollback and operations evidence are explicit",
        ),
        permission_ceiling=PERMISSIONS,
        scale=ProjectScale.LARGE,
    )
    return DynamicTeamComposer().compose(request)


def test_c4_social_scale_real_factory_graph_survives_failure_revision_restart_and_qa(
    tmp_path,
) -> None:
    store, projects, binding, task_id, graph, coordinator = _setup(tmp_path)

    assert len(graph.components) == 60
    assert len(graph.repositories) == 8
    assert projects.get(PROJECT_ID).spec_version == 2
    history = projects.spec_history(PROJECT_ID)
    assert tuple(item.spec_version for item in history) == (1, 2)
    assert history[-1].supersedes_spec_version == 1

    team = _team_plan(graph)
    implementation = [
        role for role in team.roles if role.capabilities == ("implementation",)
    ]
    assert len(implementation) == 60
    assert any(role.independent_review for role in team.roles)
    assert all(role.permissions <= PERMISSIONS for role in team.roles)

    initial_ready = {request.component_id for request in coordinator.ready_requests()}
    assert initial_ready == {
        _component_id(domain, "contract") for domain, *_ in DOMAIN_SPECS
    }

    failed_request = coordinator.start("media-contract")
    failed = coordinator.record_result(
        WorkerResultEnvelope(
            work_id=failed_request.work_id,
            component_id=failed_request.component_id,
            repository_id=failed_request.repository_id,
            base_sha=failed_request.base_sha,
            result_sha=_sha(900_001),
            diff_digest=_digest(900_001),
            coding_result=CodingResult(
                job_id=failed_request.work_id,
                failure=WorkerFailure(
                    WorkerFailureKind.PROCESS_FAILED,
                    "deterministic fixture worker failed",
                    retryable=True,
                ),
            ),
        )
    )
    assert failed.state is WorkState.REPAIR_REQUIRED
    repaired = coordinator.prepare_repair(
        "media-contract",
        base_sha=_sha(900_002),
        reason="replay bounded media worker on a clean base",
    )
    assert repaired.attempt == 2
    _accept(coordinator, "media-contract", 1)

    moderation = coordinator.start("moderation-contract")
    coordinator.record_result(_successful_envelope(moderation, 2))
    rejected = coordinator.review(
        "moderation-contract",
        ReviewDecision(
            reviewer_id="qa:moderation",
            accepted=False,
            reason="moderation acceptance evidence needs repair",
            evidence_refs=("review:moderation:reject",),
        ),
    )
    assert rejected.state is WorkState.REPAIR_REQUIRED
    coordinator.prepare_repair(
        "moderation-contract",
        base_sha=_sha(900_003),
        reason="close independent moderation QA finding",
    )
    _accept(coordinator, "moderation-contract", 3)

    ordinal = 4
    for component_id in sorted(initial_ready - {"media-contract", "moderation-contract"}):
        _accept(coordinator, component_id, ordinal)
        ordinal += 1

    host = ProductFactoryCheckpointHost(store)
    host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))
    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted_project = ProductProjectRepository(restarted_store).get(PROJECT_ID)
    binding = ProductProjectCoordinatorBinding(restarted_project, graph)
    host = ProductFactoryCheckpointHost(restarted_store)
    coordinator = host.restore_latest(host_task_id=task_id, binding=binding)

    assert _record(coordinator, "media-contract").request.attempt == 2
    assert _record(coordinator, "moderation-contract").request.attempt == 2
    assert {request.component_id for request in coordinator.ready_requests()} == {
        _component_id(domain, "storage") for domain, *_ in DOMAIN_SPECS
    }

    restart_count = 1
    while coordinator.ready_requests():
        ready = coordinator.ready_requests()
        assert len(ready) == 12
        for request in ready:
            _accept(coordinator, request.component_id, ordinal)
            ordinal += 1
        host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))
        restarted_store = SQLiteStore(restarted_store.path)
        restarted_store.initialize()
        restarted_project = ProductProjectRepository(restarted_store).get(PROJECT_ID)
        binding = ProductProjectCoordinatorBinding(restarted_project, graph)
        host = ProductFactoryCheckpointHost(restarted_store)
        coordinator = host.restore_latest(host_task_id=task_id, binding=binding)
        restart_count += 1

    assert restart_count == 5
    assert all(
        record.state is WorkState.ACCEPTED for record in coordinator.snapshot().records
    )
    assert coordinator.ready_requests() == ()

    build_release_plan = tuple(
        (
            component.component_id,
            component.repository_id,
            component.build_commands,
            component.release_identity,
        )
        for component in graph.components
    )
    assert len(build_release_plan) == 60
    assert all(
        commands and release_identity
        for _, _, commands, release_identity in build_release_plan
    )


def test_c4_social_scale_ownership_and_repository_conflicts_fail_closed() -> None:
    graph = _social_graph()
    active = OwnershipLease(
        "lease-posts-active",
        "worker-a",
        ("posts-service",),
        ("domains/posts/service",),
    )
    candidate = OwnershipLease(
        "lease-posts-candidate",
        "worker-b",
        ("posts-service",),
        ("domains/posts/service",),
    )

    conflict = graph.assess_lease(candidate, (active,))
    assert not conflict.grantable
    assert len(conflict.conflicts) == 1

    decision = IntegrationDecision(
        "decision-posts-serialize",
        IntegrationDecisionKind.SERIALIZE,
        (candidate.lease_id, active.lease_id),
        "serialize same-component repository ownership",
        ("evidence:ownership-conflict",),
    )
    resolved = graph.assess_lease(candidate, (active,), decision=decision)
    assert resolved.requires_integration

    repositories = (
        RepositoryRef("repo-one", "github", "Example/C4-Shared", "main"),
        RepositoryRef("repo-two", "GitHub", "example/c4-shared/", "main"),
    )
    with pytest.raises(RepositoryGraphError, match="aliased by multiple repository ids"):
        ProductRepositoryGraph(
            PROJECT_ID,
            repositories,
            (
                ProductComponent("one", "repo-one", ("one",)),
                ProductComponent("two", "repo-two", ("two",)),
            ),
        )


class _CountingDependencies(dict[str, int]):
    def __init__(self, initial: dict[str, int]) -> None:
        super().__init__(initial)
        self.writes = 0

    def __setitem__(self, key: str, value: int) -> None:
        self.writes += 1
        super().__setitem__(key, value)


def test_c4_readiness_propagation_work_is_bounded_by_dependency_edges(tmp_path) -> None:
    _store, _projects, _binding, _task_id, graph, coordinator = _setup(tmp_path)
    edge_count = sum(len(component.dependencies) for component in graph.components)
    counter = _CountingDependencies(dict(coordinator._remaining_dependencies))
    coordinator._remaining_dependencies = counter

    ordinal = 1
    while coordinator.ready_requests():
        for request in coordinator.ready_requests():
            _accept(coordinator, request.component_id, ordinal)
            ordinal += 1

    assert counter.writes == edge_count
    assert edge_count < len(graph.components) * 4
    assert all(
        record.state is WorkState.ACCEPTED for record in coordinator.snapshot().records
    )


def _service(
    service_id: str,
    *,
    wave: int,
    dependencies: tuple[str, ...] = (),
) -> DeployableService:
    return DeployableService(
        service_id=service_id,
        project_id=PROJECT_ID,
        environment_id="staging-c4",
        release_sha=_sha(700_000 + wave),
        wave=wave,
        replicas=(
            ServiceReplica(f"{service_id}-r1", f"{service_id}-node-a"),
            ServiceReplica(f"{service_id}-r2", f"{service_id}-node-b"),
        ),
        min_healthy_replicas=2,
        dependencies=dependencies,
    )


def _observe_healthy(
    operations: ProductOperationsCoordinator,
    service: DeployableService,
    *,
    evidence: str,
) -> None:
    operations.record_observation(
        ServiceObservation(
            service.service_id,
            service.release_sha,
            tuple(replica.replica_id for replica in service.replicas),
            (),
            (evidence,),
            datetime(2026, 8, 24, 6, 0, tzinfo=UTC),
        )
    )


def test_c4_operations_node_loss_opens_durable_incident() -> None:
    operations = ProductOperationsCoordinator(PROJECT_ID)
    services = (
        _service("identity-api", wave=0),
        _service("feed-api", wave=1, dependencies=("identity-api",)),
        _service("search-api", wave=1, dependencies=("identity-api",)),
        _service(
            "desktop-client",
            wave=2,
            dependencies=("feed-api", "search-api"),
        ),
        _service("operations-api", wave=3, dependencies=("desktop-client",)),
    )
    for service in services:
        operations.register(service)
        _observe_healthy(
            operations,
            service,
            evidence=f"health:{service.service_id}:baseline",
        )

    feed = services[1]
    operations.record_node_availability("feed-api-node-b", available=False)
    degraded = operations.record_observation(
        ServiceObservation(
            feed.service_id,
            feed.release_sha,
            ("feed-api-r1",),
            ("feed-api-r2",),
            ("health:feed-api:node-loss",),
            datetime(2026, 8, 24, 6, 5, tzinfo=UTC),
        )
    )
    assert degraded.health is ServiceHealth.DEGRADED
    assert degraded.node_loss == ("feed-api-r2",)
    assert not operations.health_summary().release_ready

    incidents = IncidentRepairReleaseCoordinator(PROJECT_ID)
    incident = incidents.open_incident(
        "incident-feed-node-loss",
        IncidentTrigger(
            PROJECT_ID,
            "feed-api",
            "staging-c4",
            feed.release_sha,
            IncidentKind.HEALTH,
            IncidentSeverity.HIGH,
            ("health:feed-api:node-loss",),
            "approval:incident-feed-node-loss",
            datetime(2026, 8, 24, 6, 6, tzinfo=UTC),
        ),
        operations.snapshot(),
    )
    assert incident.state is IncidentState.OPEN

    operations_snapshot = operations.snapshot()
    incident_snapshot = incidents.snapshot()
    restored_operations = ProductOperationsCoordinator(PROJECT_ID)
    restored_operations.restore(operations_snapshot)
    restored_incidents = IncidentRepairReleaseCoordinator(PROJECT_ID)
    restored_incidents.restore(incident_snapshot)

    assert restored_operations.health_summary().degraded == ("feed-api",)
    assert restored_incidents.get("incident-feed-node-loss").state is IncidentState.OPEN
