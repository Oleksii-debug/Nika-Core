from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_multi_repository import (
    MultiRepositoryProductFactoryHost,
    RepositoryGraphIntegrityError,
)
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from nika_core.product_factory_packaged_preparation import (
    PRODUCT_FACTORY_HOST_AGENT_ID,
    PackagedProductFactoryExecutionPlan,
    PackagedProductFactoryPreparationError,
    PackagedProductFactoryPreparationService,
    product_factory_host_task_identity,
)
from nika_core.product_project import ProductProjectRepository, ProductProjectSpec
from nika_core.toolsmith.contracts import RecoveryState


class NeverDispatchWorker:
    async def dispatch(self, request):
        raise AssertionError(f"unexpected dispatch: {request.work_id}")

    async def inspect(self, work_id: str) -> RecoveryState | None:
        raise AssertionError(f"unexpected inspect: {work_id}")

    async def recover(self, request, state):
        raise AssertionError(f"unexpected recover: {request.work_id}:{state}")


def _fixture(tmp_path: Path):
    store = SQLiteStore(tmp_path / "product factory підготовка.db")
    store.initialize()
    repository = ProductProjectRepository(store)
    locator = "Oleksii-debug/Nika-Core"
    project = repository.create(
        project_id="product-preparation",
        name="Packaged Product Factory preparation",
        spec=ProductProjectSpec(
            goal="Prepare one trusted repository component",
            desired_outcome="Durable Product Factory authority exists",
            repository_refs=(locator,),
        ),
        idempotency_key="create:product-preparation",
    )
    graph = ProductRepositoryGraph(
        project_id=project.project_id,
        repositories=(
            RepositoryRef(
                repository_id="repo-core",
                provider="github",
                locator=locator,
                default_branch="main",
            ),
        ),
        components=(
            ProductComponent(
                component_id="core",
                repository_id="repo-core",
                paths=("src/nika_core",),
                test_commands=(("python", "-m", "pytest", "tests"),),
            ),
        ),
    )
    base_shas = {"repo-core": "a" * 40}
    component_goals = {"core": "Implement the exact accepted ProductProject work"}
    plan = PackagedProductFactoryExecutionPlan(
        project_id=project.project_id,
        expected_spec_version=project.spec_version,
        expected_row_version=project.row_version,
        graph=graph,
        graph_version=1,
        base_shas=base_shas,
        component_goals=component_goals,
        permission_ceiling=frozenset({"read_source", "write_source", "run_tests"}),
    )
    tasks = TaskQueue(store)
    host = MultiRepositoryProductFactoryHost(store, NeverDispatchWorker())
    service = PackagedProductFactoryPreparationService(
        repository=repository,
        tasks=tasks,
        host=host,
        workspace_id="packaged.product-factory",
    )
    return store, repository, tasks, service, project, graph, plan, base_shas, component_goals


def _task_count(store: SQLiteStore) -> int:
    with store.connection() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])


def test_prepare_is_exact_idempotent_and_restart_restores_same_authority(
    tmp_path: Path,
) -> None:
    store, repository, _tasks, service, project, _graph, plan, _bases, _goals = _fixture(
        tmp_path
    )

    first = service.prepare(plan)
    second = service.prepare(plan)

    assert first.host_task_id == second.host_task_id
    assert first.graph_digest == second.graph_digest
    assert first.state.coordinator.snapshot() == second.state.coordinator.snapshot()
    assert _task_count(store) == 1

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted = PackagedProductFactoryPreparationService(
        repository=ProductProjectRepository(restarted_store),
        tasks=TaskQueue(restarted_store),
        host=MultiRepositoryProductFactoryHost(restarted_store, NeverDispatchWorker()),
        workspace_id="packaged.product-factory",
    ).restore(project.project_id)

    assert restarted.host_task_id == first.host_task_id
    assert restarted.graph_digest == first.graph_digest
    assert restarted.state.coordinator.snapshot() == first.state.coordinator.snapshot()
    assert repository.get(project.project_id).row_version == project.row_version


def test_prepare_recovers_after_exact_host_task_exists_without_graph_checkpoint(
    tmp_path: Path,
) -> None:
    store, _repository, tasks, service, project, _graph, plan, _bases, _goals = _fixture(
        tmp_path
    )
    task_id = product_factory_host_task_identity(
        project.project_id,
        spec_version=project.spec_version,
        row_version=project.row_version,
    )
    tasks.create_exact(
        task_id=task_id,
        workspace_id="packaged.product-factory",
        agent_id=PRODUCT_FACTORY_HOST_AGENT_ID,
        payload={
            "kind": "product_factory",
            "product_project_id": project.project_id,
        },
    )

    prepared = service.prepare(plan)

    assert prepared.host_task_id == task_id
    assert prepared.state.authority.project_id == project.project_id
    assert _task_count(store) == 1


def test_same_project_version_rejects_competing_repository_graph(
    tmp_path: Path,
) -> None:
    store, _repository, _tasks, service, project, _graph, plan, _bases, _goals = _fixture(
        tmp_path
    )
    service.prepare(plan)
    competing_graph = ProductRepositoryGraph(
        project_id=project.project_id,
        repositories=plan.graph.repositories,
        components=(
            ProductComponent(
                component_id="core",
                repository_id="repo-core",
                paths=("src/other",),
                test_commands=(("python", "-m", "pytest", "tests"),),
            ),
        ),
    )
    competing = PackagedProductFactoryExecutionPlan(
        project_id=project.project_id,
        expected_spec_version=project.spec_version,
        expected_row_version=project.row_version,
        graph=competing_graph,
        graph_version=1,
        base_shas={"repo-core": "a" * 40},
        component_goals={"core": "Implement the exact accepted ProductProject work"},
        permission_ceiling=frozenset({"read_source", "write_source", "run_tests"}),
    )

    with pytest.raises(
        RepositoryGraphIntegrityError,
        match="authority does not match candidate graph",
    ):
        service.prepare(competing)

    assert _task_count(store) == 1


def test_stale_trusted_plan_fails_before_host_task_creation(tmp_path: Path) -> None:
    store, repository, _tasks, service, project, _graph, plan, _bases, _goals = _fixture(
        tmp_path
    )
    repository.update_spec(
        project.project_id,
        replace(
            project.spec,
            desired_outcome="A newer ProductProject outcome",
        ),
        expected_row_version=project.row_version,
        change_reason="test stale packaged preparation plan",
    )

    with pytest.raises(
        PackagedProductFactoryPreparationError,
        match="stale",
    ):
        service.prepare(plan)

    assert _task_count(store) == 0


def test_execution_plan_snapshots_mutable_graph_and_mapping_inputs(tmp_path: Path) -> None:
    (
        _store,
        _repository,
        _tasks,
        service,
        project,
        graph,
        plan,
        base_shas,
        component_goals,
    ) = _fixture(tmp_path)
    base_shas["repo-core"] = "b" * 40
    component_goals["core"] = "mutated caller goal"
    graph.components = (
        ProductComponent(
            component_id="core",
            repository_id="repo-core",
            paths=("src/mutated",),
            test_commands=(("python", "-m", "pytest", "tests"),),
        ),
    )

    prepared = service.prepare(plan)

    request = prepared.state.coordinator.snapshot().records[0].request
    assert request.base_sha == "a" * 40
    assert request.goal == "Implement the exact accepted ProductProject work"
    assert request.allowed_paths == ("src/nika_core",)


def test_deterministic_host_task_collision_fails_closed(tmp_path: Path) -> None:
    (
        store,
        _repository,
        tasks,
        service,
        project,
        _graph,
        plan,
        _bases,
        _goals,
    ) = _fixture(tmp_path)
    task_id = product_factory_host_task_identity(
        project.project_id,
        spec_version=project.spec_version,
        row_version=project.row_version,
    )
    tasks.create_exact(
        task_id=task_id,
        workspace_id="other-workspace",
        agent_id=PRODUCT_FACTORY_HOST_AGENT_ID,
        payload={
            "kind": "product_factory",
            "product_project_id": project.project_id,
        },
    )

    with pytest.raises(
        PackagedProductFactoryPreparationError,
        match="conflicts with existing authority",
    ):
        service.prepare(plan)

    assert _task_count(store) == 1


def test_require_repair_request_never_upgrades_nonfailed_work(tmp_path: Path) -> None:
    _store, _repository, _tasks, service, project, _graph, plan, _bases, _goals = _fixture(
        tmp_path
    )
    prepared = service.prepare(plan)
    record = prepared.state.coordinator.snapshot().records[0]
    assert record.state.value == "ready"

    with pytest.raises(
        PackagedProductFactoryPreparationError,
        match="not an exact durable REPAIR_REQUIRED",
    ):
        service.require_repair_request(project.project_id, "core")


def test_execution_plan_rejects_digest_length_as_repository_base_sha(
    tmp_path: Path,
) -> None:
    (
        _store,
        _repository,
        _tasks,
        _service,
        project,
        graph,
        _plan,
        _bases,
        _goals,
    ) = _fixture(tmp_path)

    with pytest.raises(
        PackagedProductFactoryPreparationError,
        match="base SHA is invalid",
    ):
        PackagedProductFactoryExecutionPlan(
            project_id=project.project_id,
            expected_spec_version=project.spec_version,
            expected_row_version=project.row_version,
            graph=graph,
            graph_version=1,
            base_shas={"repo-core": "a" * 64},
            component_goals={"core": "Implement the exact accepted ProductProject work"},
            permission_ceiling=frozenset({"read_source", "write_source", "run_tests"}),
        )
