from __future__ import annotations

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_project_binding import (
    ProductProjectBindingError,
    ProductProjectCoordinatorBinding,
    StaleProductProjectBindingError,
)
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from nika_core.product_project import (
    ProductProjectRepository,
    ProductProjectSpec,
    ProductRequirement,
)

SHA_A = "a" * 40
PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})
LOCATOR = "org/repo"


def _spec(goal: str = "Build accessible product") -> ProductProjectSpec:
    return ProductProjectSpec(
        goal=goal,
        desired_outcome="A reviewed product",
        requirements=(
            ProductRequirement(
                "req-1",
                "Keyboard operation",
                ("All primary actions keyboard reachable",),
            ),
        ),
        repository_refs=(LOCATOR,),
    )


def _project_repo(tmp_path) -> ProductProjectRepository:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    return ProductProjectRepository(store)


def _graph(project_id: str = "p1", locator: str = LOCATOR) -> ProductRepositoryGraph:
    return ProductRepositoryGraph(
        project_id=project_id,
        repositories=(RepositoryRef("repo-1", "github", locator, "main"),),
        components=(
            ProductComponent(
                component_id="core",
                repository_id="repo-1",
                paths=("src/core",),
                test_commands=(("python", "-m", "pytest", "tests/core"),),
            ),
            ProductComponent(
                component_id="docs",
                repository_id="repo-1",
                paths=("docs/product",),
                test_commands=(("python", "-m", "pytest", "tests/docs"),),
            ),
        ),
    )


def _binding(tmp_path) -> tuple[ProductProjectRepository, ProductProjectCoordinatorBinding]:
    repo = _project_repo(tmp_path)
    project = repo.create(
        project_id="p1",
        name="Product",
        spec=_spec(),
        idempotency_key="create:p1",
    )
    return repo, ProductProjectCoordinatorBinding(project, _graph())


def test_binding_plans_from_real_durable_product_project(tmp_path) -> None:
    _, binding = _binding(tmp_path)

    coordinator = binding.plan(
        base_shas={"repo-1": SHA_A},
        component_goals={"core": "build core", "docs": "write docs"},
        permission_ceiling=PERMISSIONS,
    )

    assert coordinator.snapshot().project_id == binding.project.project_id
    assert {item.component_id for item in coordinator.ready_requests()} == {"core", "docs"}


def test_checkpoint_round_trip_preserves_coordinator_state(tmp_path) -> None:
    _, binding = _binding(tmp_path)
    coordinator = binding.plan(
        base_shas={"repo-1": SHA_A},
        component_goals={"core": "build core", "docs": "write docs"},
        permission_ceiling=PERMISSIONS,
    )
    started = coordinator.start("core")

    restored = binding.restore(binding.checkpoint(coordinator))

    snapshot = restored.snapshot()
    core = next(record for record in snapshot.records if record.request.component_id == "core")
    assert core.request.work_id == started.work_id
    assert core.state.value == "running"
    assert {item.component_id for item in restored.ready_requests()} == {"docs"}


def test_product_spec_change_invalidates_old_checkpoint(tmp_path) -> None:
    repo, binding = _binding(tmp_path)
    coordinator = binding.plan(
        base_shas={"repo-1": SHA_A},
        component_goals={"core": "build core", "docs": "write docs"},
        permission_ceiling=PERMISSIONS,
    )
    checkpoint = binding.checkpoint(coordinator)
    updated = repo.update_spec(
        "p1",
        _spec("Build accessible product v2"),
        expected_row_version=binding.project.row_version,
    )
    refreshed = ProductProjectCoordinatorBinding(updated, _graph())

    with pytest.raises(StaleProductProjectBindingError, match="explicit reconciliation"):
        refreshed.restore(checkpoint)


def test_repository_graph_must_be_declared_by_product_project(tmp_path) -> None:
    repo = _project_repo(tmp_path)
    project = repo.create(
        project_id="p1",
        name="Product",
        spec=_spec(),
        idempotency_key="create:p1",
    )

    with pytest.raises(ProductProjectBindingError, match="not declared"):
        ProductProjectCoordinatorBinding(project, _graph(locator="other/repo"))


def test_project_identity_mismatch_fails_before_orchestration(tmp_path) -> None:
    repo = _project_repo(tmp_path)
    project = repo.create(
        project_id="p1",
        name="Product",
        spec=_spec(),
        idempotency_key="create:p1",
    )

    with pytest.raises(ProductProjectBindingError, match="project_id"):
        ProductProjectCoordinatorBinding(project, _graph(project_id="p2"))
