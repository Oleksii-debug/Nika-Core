from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_checkpoint_host import (
    ProductFactoryCheckpointHost,
    ProductFactoryCheckpointIntegrityError,
)
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from nika_core.product_factory_project_binding import ProductProjectCoordinatorBinding
from nika_core.product_project import (
    ProductProjectRepository,
    ProductProjectSpec,
    ProductRequirement,
)

_SOURCE_SHA = "a" * 40
_LOCATOR = "org/pf12-exact-id"
_PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})


def _setup(tmp_path: Path):
    store = SQLiteStore(tmp_path / "pf12-exact-id.db")
    store.initialize()
    project = ProductProjectRepository(store).create(
        project_id="pf12-exact-id-project",
        name="PF12 exact ID authority",
        spec=ProductProjectSpec(
            goal="Keep exact checkpoint reads host-authoritative",
            desired_outcome="Only the independently committed host head is loadable",
            requirements=(
                ProductRequirement(
                    "req-exact-id",
                    "Exact checkpoint IDs cannot mint historical authority",
                    ("Non-head checkpoint IDs fail closed",),
                ),
            ),
            repository_refs=(_LOCATOR,),
        ),
        idempotency_key="pf12:exact-id:create",
    )
    graph = ProductRepositoryGraph(
        project_id=project.project_id,
        repositories=(RepositoryRef("repo-1", "github", _LOCATOR, "main"),),
        components=(
            ProductComponent(
                component_id="core",
                repository_id="repo-1",
                paths=("src/core",),
                test_commands=(("python", "-m", "pytest", "tests/core"),),
            ),
        ),
    )
    binding = ProductProjectCoordinatorBinding(project, graph)
    coordinator = binding.plan(
        base_shas={"repo-1": _SOURCE_SHA},
        component_goals={"core": "build core"},
        permission_ceiling=_PERMISSIONS,
    )
    task = TaskQueue(store).create(
        workspace_id="pf12-exact-id",
        agent_id="product-factory",
        payload={
            "kind": "product_factory",
            "product_project_id": project.project_id,
        },
    )
    return store, binding, coordinator, task.task_id


def test_exact_id_load_accepts_only_current_committed_host_head(tmp_path: Path) -> None:
    store, binding, coordinator, task_id = _setup(tmp_path)
    host = ProductFactoryCheckpointHost(store)

    first = host.save(
        host_task_id=task_id,
        checkpoint=binding.checkpoint(coordinator),
    )
    coordinator.start("core")
    second = host.save(
        host_task_id=task_id,
        checkpoint=binding.checkpoint(coordinator),
    )

    loaded = host.load(second.checkpoint_id, host_task_id=task_id)
    assert loaded.checkpoint_id == second.checkpoint_id
    assert loaded.checkpoint.coordinator.revision > first.checkpoint.coordinator.revision

    with pytest.raises(
        ProductFactoryCheckpointIntegrityError,
        match="canonical committed host-task head",
    ):
        host.load(first.checkpoint_id, host_task_id=task_id)

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted = ProductFactoryCheckpointHost(restarted_store)
    with pytest.raises(
        ProductFactoryCheckpointIntegrityError,
        match="canonical committed host-task head",
    ):
        restarted.load(first.checkpoint_id, host_task_id=task_id)
    loaded_after_restart = restarted.load(
        second.checkpoint_id,
        host_task_id=task_id,
    )
    assert loaded_after_restart.checkpoint_id == second.checkpoint_id
