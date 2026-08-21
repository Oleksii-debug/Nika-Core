from __future__ import annotations

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_checkpoint_host import (
    ProductFactoryCheckpointHost,
    ProductFactoryRecoveryDisposition,
)
from nika_core.product_factory_coordinator import (
    CoordinatorError,
    ProductFactoryCoordinator,
    ReviewDecision,
    WorkerResultEnvelope,
    WorkState,
)
from nika_core.product_factory_orchestration import (
    ComponentBrief,
    DynamicTeamComposer,
    ProductComponent,
    ProductRepositoryGraph,
    ProjectScale,
    RepositoryRef,
    TeamCompositionRequest,
)
from nika_core.product_factory_project_binding import ProductProjectCoordinatorBinding
from nika_core.product_project import (
    ProductProjectRepository,
    ProductProjectSpec,
    ProductRequirement,
)
from nika_core.toolsmith.contracts import CodingResult
from nika_core.toolsmith.contracts import TestEvidence as WorkerTestEvidence

PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})
KINDS = ("backend", "web", "desktop", "data", "infra")


def _sha(index: int) -> str:
    return f"{index:040x}"[-40:]


def _digest(index: int) -> str:
    return f"{index:064x}"[-64:]


def _graph(component_count: int, repository_count: int) -> ProductRepositoryGraph:
    repositories = tuple(
        RepositoryRef(
            repository_id=f"repo-{index:02d}",
            provider="github",
            locator=f"org/scale-repo-{index:02d}",
            default_branch="main",
        )
        for index in range(repository_count)
    )
    components = []
    for index in range(component_count):
        repository_index = index % repository_count
        dependency = (
            ()
            if index < repository_count
            else (f"component-{index - repository_count:03d}",)
        )
        components.append(
            ProductComponent(
                component_id=f"component-{index:03d}",
                repository_id=f"repo-{repository_index:02d}",
                paths=(f"components/component-{index:03d}",),
                dependencies=dependency,
                build_commands=(("python", "-m", "compileall", f"component_{index:03d}"),),
                test_commands=(("python", "-m", "pytest", f"tests/component_{index:03d}"),),
                release_identity=f"component-{index:03d}:v1",
            )
        )
    return ProductRepositoryGraph(
        project_id="p-scale",
        repositories=repositories,
        components=tuple(components),
    )


def _spec(graph: ProductRepositoryGraph, goal: str = "Build a large product") -> ProductProjectSpec:
    return ProductProjectSpec(
        goal=goal,
        desired_outcome="All bounded components independently reviewed",
        requirements=(
            ProductRequirement(
                "req-scale",
                "Every component has deterministic test and review evidence",
                ("All components reach accepted only after independent review",),
            ),
        ),
        repository_refs=tuple(repository.locator for repository in graph.repositories),
    )


def _setup(tmp_path, component_count: int, repository_count: int):
    graph = _graph(component_count, repository_count)
    store = SQLiteStore(tmp_path / "nika-scale.db")
    store.initialize()
    projects = ProductProjectRepository(store)
    project = projects.create(
        project_id="p-scale",
        name="Scale Product",
        spec=_spec(graph),
        idempotency_key="create:p-scale",
    )
    binding = ProductProjectCoordinatorBinding(project, graph)
    task = TaskQueue(store).create(
        workspace_id="ws-scale",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": project.project_id},
    )
    return store, projects, binding, task.task_id, graph


def _planned(
    binding: ProductProjectCoordinatorBinding,
    graph: ProductRepositoryGraph,
) -> ProductFactoryCoordinator:
    return binding.plan(
        base_shas={repository.repository_id: _sha(index + 1) for index, repository in enumerate(graph.repositories)},
        component_goals={
            component.component_id: f"Implement {component.component_id}"
            for component in graph.components
        },
        permission_ceiling=PERMISSIONS,
    )


def _record(coordinator: ProductFactoryCoordinator, component_id: str):
    return next(
        record
        for record in coordinator.snapshot().records
        if record.request.component_id == component_id
    )


def _successful_envelope(request, ordinal: int) -> WorkerResultEnvelope:
    result = CodingResult(
        job_id=request.work_id,
        test_evidence=(
            WorkerTestEvidence(
                command=request.acceptance_commands[0],
                exit_code=0,
                output_digest=_digest(ordinal + 1),
            ),
        ),
    )
    return WorkerResultEnvelope(
        work_id=request.work_id,
        component_id=request.component_id,
        repository_id=request.repository_id,
        base_sha=request.base_sha,
        result_sha=_sha(100_000 + ordinal),
        diff_digest=_digest(200_000 + ordinal),
        coding_result=result,
    )


def _accept(coordinator: ProductFactoryCoordinator, component_id: str, ordinal: int) -> None:
    request = coordinator.start(component_id)
    updated = coordinator.record_result(_successful_envelope(request, ordinal))
    assert updated.state is WorkState.REVIEW_REQUIRED
    reviewed = coordinator.review(
        component_id,
        ReviewDecision(
            reviewer_id=f"qa-{component_id}",
            accepted=True,
            reason="independent scale qualification passed",
            evidence_refs=(f"evidence:{component_id}",),
        ),
    )
    assert reviewed.state is WorkState.ACCEPTED


@pytest.mark.parametrize(
    ("component_count", "repository_count"),
    ((1, 1), (5, 3), (25, 10), (100, 10)),
)
def test_scale_planning_keeps_bounded_unique_work_and_parallel_repo_waves(
    tmp_path,
    component_count: int,
    repository_count: int,
) -> None:
    _, _, binding, _, graph = _setup(tmp_path, component_count, repository_count)

    coordinator = _planned(binding, graph)
    snapshot = coordinator.snapshot()

    assert len(snapshot.records) == component_count
    work_ids = {record.request.work_id for record in snapshot.records}
    assert len(work_ids) == component_count
    assert all(record.request.project_id == "p-scale" for record in snapshot.records)
    assert all(record.request.permission_ceiling == PERMISSIONS for record in snapshot.records)
    assert all(len(record.request.allowed_paths) == 1 for record in snapshot.records)
    assert len(coordinator.ready_requests()) == min(component_count, repository_count)
    assert max(len(record.request.acceptance_commands) for record in snapshot.records) == 1


def test_large_dynamic_team_fans_out_implementation_and_attenuates_new_specialist() -> None:
    graph = _graph(100, 10)
    request = TeamCompositionRequest(
        project_id=graph.project_id,
        components=tuple(
            ComponentBrief(
                component.component_id,
                KINDS[index % len(KINDS)],
                frozenset({"accessibility"}) if index % 17 == 0 else frozenset(),
            )
            for index, component in enumerate(graph.components)
        ),
        acceptance_criteria=(
            "All surfaces are accessible and independently reviewed",
            "Every component has deterministic tests",
        ),
        permission_ceiling=PERMISSIONS,
        scale=ProjectScale.LARGE,
    )
    composer = DynamicTeamComposer()

    plan = composer.compose(request)
    implementation_roles = [
        role for role in plan.roles if role.capabilities == ("implementation",)
    ]

    assert len(implementation_roles) == 100
    assert any(role.independent_review for role in plan.roles)
    assert all(role.permissions <= PERMISSIONS for role in plan.roles)
    original_ids = tuple(role.role_id for role in plan.roles)

    expanded = composer.add_specialist(
        plan,
        specialization="protocol-specialist",
        component_ids=("component-010", "component-011"),
        requested_permissions=("read_source", "write_source", "deploy_production"),
        reason="late protocol requirement",
        evidence_refs=("decision:protocol-specialist",),
    )

    assert tuple(role.role_id for role in expanded.roles[:-1]) == original_ids
    assert expanded.roles[-1].permissions == frozenset({"read_source", "write_source"})
    assert "deploy_production" not in expanded.roles[-1].permissions


def test_hundred_component_project_survives_ten_restart_waves_to_completion(tmp_path) -> None:
    store, _, binding, task_id, graph = _setup(tmp_path, 100, 10)
    coordinator = _planned(binding, graph)
    host = ProductFactoryCheckpointHost(store)
    revisions: list[int] = []
    accepted_count = 0
    wave_count = 0

    while coordinator.ready_requests():
        ready = coordinator.ready_requests()
        assert 1 <= len(ready) <= 10
        for request in ready:
            _accept(coordinator, request.component_id, accepted_count)
            accepted_count += 1
        saved = host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))
        revisions.append(saved.checkpoint.coordinator.revision)
        wave_count += 1

        restarted_store = SQLiteStore(store.path)
        restarted_store.initialize()
        restarted_project = ProductProjectRepository(restarted_store).get("p-scale")
        restarted_binding = ProductProjectCoordinatorBinding(restarted_project, graph)
        restarted_host = ProductFactoryCheckpointHost(restarted_store)
        candidate = restarted_host.inspect_latest(
            host_task_id=task_id,
            binding=restarted_binding,
        )
        assert candidate.disposition is ProductFactoryRecoveryDisposition.RESUMABLE
        coordinator = restarted_host.restore_latest(
            host_task_id=task_id,
            binding=restarted_binding,
        )
        store = restarted_store
        binding = restarted_binding
        host = restarted_host

    assert wave_count == 10
    assert accepted_count == 100
    assert revisions == sorted(revisions)
    assert len(set(revisions)) == len(revisions)
    assert all(record.state is WorkState.ACCEPTED for record in coordinator.snapshot().records)
    assert coordinator.ready_requests() == ()


def test_blocked_subgraph_survives_restart_while_independent_chain_advances(tmp_path) -> None:
    store, _, binding, task_id, graph = _setup(tmp_path, 4, 2)
    coordinator = _planned(binding, graph)
    host = ProductFactoryCheckpointHost(store)

    coordinator.block("component-000", "upstream contract unavailable")
    _accept(coordinator, "component-001", 1)
    host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    project = ProductProjectRepository(restarted_store).get("p-scale")
    restarted_binding = ProductProjectCoordinatorBinding(project, graph)
    restored = ProductFactoryCheckpointHost(restarted_store).restore_latest(
        host_task_id=task_id,
        binding=restarted_binding,
    )

    assert _record(restored, "component-000").state is WorkState.BLOCKED
    assert _record(restored, "component-002").state is WorkState.PLANNED
    assert _record(restored, "component-001").state is WorkState.ACCEPTED
    assert _record(restored, "component-003").state is WorkState.READY
    assert {request.component_id for request in restored.ready_requests()} == {"component-003"}


def test_review_rejection_repair_attempt_and_new_work_identity_survive_restarts(tmp_path) -> None:
    store, _, binding, task_id, graph = _setup(tmp_path, 5, 3)
    coordinator = _planned(binding, graph)
    request = coordinator.start("component-000")
    coordinator.record_result(_successful_envelope(request, 1))
    rejected = coordinator.review(
        "component-000",
        ReviewDecision(
            reviewer_id="qa-independent",
            accepted=False,
            reason="security review requires repair",
            evidence_refs=("review:security-reject",),
        ),
    )
    assert rejected.state is WorkState.REPAIR_REQUIRED
    host = ProductFactoryCheckpointHost(store)
    host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    project = ProductProjectRepository(restarted_store).get("p-scale")
    binding = ProductProjectCoordinatorBinding(project, graph)
    host = ProductFactoryCheckpointHost(restarted_store)
    coordinator = host.restore_latest(host_task_id=task_id, binding=binding)

    repaired = coordinator.prepare_repair(
        "component-000",
        base_sha=_sha(9_999),
        reason="apply independent security findings",
    )
    assert repaired.attempt == 2
    assert repaired.work_id != request.work_id
    assert repaired.base_sha == _sha(9_999)
    host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))

    second_restart = SQLiteStore(restarted_store.path)
    second_restart.initialize()
    project = ProductProjectRepository(second_restart).get("p-scale")
    binding = ProductProjectCoordinatorBinding(project, graph)
    coordinator = ProductFactoryCheckpointHost(second_restart).restore_latest(
        host_task_id=task_id,
        binding=binding,
    )

    second_request = coordinator.start("component-000")
    coordinator.record_result(_successful_envelope(second_request, 2))
    accepted = coordinator.review(
        "component-000",
        ReviewDecision(
            reviewer_id="qa-independent",
            accepted=True,
            reason="repair closes independent findings",
            evidence_refs=("review:security-accept",),
        ),
    )
    assert accepted.state is WorkState.ACCEPTED
    assert accepted.request.attempt == 2


def test_stale_worker_result_fails_closed_and_running_identity_survives_restart(tmp_path) -> None:
    store, _, binding, task_id, graph = _setup(tmp_path, 25, 10)
    coordinator = _planned(binding, graph)
    request = coordinator.start("component-000")
    valid = _successful_envelope(request, 1)
    stale = WorkerResultEnvelope(
        work_id=valid.work_id,
        component_id=valid.component_id,
        repository_id=valid.repository_id,
        base_sha=_sha(88_888),
        result_sha=valid.result_sha,
        diff_digest=valid.diff_digest,
        coding_result=valid.coding_result,
    )

    with pytest.raises(CoordinatorError, match="stale worker result"):
        coordinator.record_result(stale)
    assert _record(coordinator, "component-000").state is WorkState.RUNNING

    host = ProductFactoryCheckpointHost(store)
    host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))
    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    project = ProductProjectRepository(restarted_store).get("p-scale")
    binding = ProductProjectCoordinatorBinding(project, graph)
    restored = ProductFactoryCheckpointHost(restarted_store).restore_latest(
        host_task_id=task_id,
        binding=binding,
    )

    record = _record(restored, "component-000")
    assert record.state is WorkState.RUNNING
    assert record.request.work_id == request.work_id
    assert record.request.base_sha == request.base_sha


def test_scale_checkpoint_payload_keeps_exact_repository_component_and_test_identity(tmp_path) -> None:
    store, _, binding, task_id, graph = _setup(tmp_path, 100, 10)
    coordinator = _planned(binding, graph)
    host = ProductFactoryCheckpointHost(store)

    for ordinal, request in enumerate(coordinator.ready_requests()):
        _accept(coordinator, request.component_id, ordinal)
    saved = host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))
    restored = host.restore_latest(host_task_id=task_id, binding=binding)

    assert saved.checkpoint.project_id == "p-scale"
    assert len(restored.snapshot().records) == 100
    for original, recovered in zip(
        coordinator.snapshot().records,
        restored.snapshot().records,
        strict=True,
    ):
        assert recovered.request.work_id == original.request.work_id
        assert recovered.request.repository_id == original.request.repository_id
        assert recovered.request.allowed_paths == original.request.allowed_paths
        assert recovered.request.acceptance_commands == original.request.acceptance_commands
        assert recovered.state is original.state
