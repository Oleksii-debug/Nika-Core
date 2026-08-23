from __future__ import annotations

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_checkpoint_host import (
    ProductFactoryCheckpointHost,
    ProductFactoryTrustedPlanAuthorityError,
)
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from nika_core.product_factory_project_binding import (
    ProductProjectCoordinatorBinding,
    ProductProjectCoordinatorCheckpoint,
)
from nika_core.product_project import (
    ProductProjectRepository,
    ProductProjectSpec,
    ProductRequirement,
)

SHA_A = "a" * 40
LOCATOR = "org/repo"
PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})


def _spec() -> ProductProjectSpec:
    return ProductProjectSpec(
        goal="Build accessible product",
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


def _graph() -> ProductRepositoryGraph:
    return ProductRepositoryGraph(
        project_id="p1",
        repositories=(RepositoryRef("repo-1", "github", LOCATOR, "main"),),
        components=(
            ProductComponent(
                component_id="core",
                repository_id="repo-1",
                paths=("src/core",),
                test_commands=(("python", "-m", "pytest", "tests/core"),),
            ),
        ),
    )


def _setup(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    project = ProductProjectRepository(store).create(
        project_id="p1",
        name="Product",
        spec=_spec(),
        idempotency_key="create:p1",
    )
    binding = ProductProjectCoordinatorBinding(project, _graph())
    task = TaskQueue(store).create(
        workspace_id="ws-product",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": "p1"},
    )
    return store, binding, task.task_id


def _planned(binding: ProductProjectCoordinatorBinding, *, goal: str = "build core"):
    return binding.plan(
        base_shas={"repo-1": SHA_A},
        component_goals={"core": goal},
        permission_ceiling=PERMISSIONS,
    )


def test_candidate_checkpoint_cannot_supply_its_own_bootstrap_authority(tmp_path) -> None:
    store, binding, task_id = _setup(tmp_path)
    candidate = _planned(binding, goal="candidate-controlled plan")
    untrusted_checkpoint = ProductProjectCoordinatorCheckpoint(
        project_id=binding.project.project_id,
        spec_version=binding.project.spec_version,
        row_version=binding.project.row_version,
        coordinator=candidate.snapshot(),
    )

    assert untrusted_checkpoint.trusted_plan_fingerprint is None
    with pytest.raises(
        ProductFactoryTrustedPlanAuthorityError,
        match="first Product Factory checkpoint requires live trusted plan authority",
    ):
        ProductFactoryCheckpointHost(store).save(
            host_task_id=task_id,
            checkpoint=untrusted_checkpoint,
        )


def test_checkpoint_constructor_rejects_candidate_controlled_authority_keyword(tmp_path) -> None:
    _, binding, _ = _setup(tmp_path)
    coordinator = _planned(binding)

    with pytest.raises(TypeError, match="trusted_plan_fingerprint"):
        ProductProjectCoordinatorCheckpoint(
            project_id=binding.project.project_id,
            spec_version=binding.project.spec_version,
            row_version=binding.project.row_version,
            coordinator=coordinator.snapshot(),
            trusted_plan_fingerprint=coordinator.trusted_plan_fingerprint,
        )


def test_live_binding_authority_is_ephemeral_and_not_rehydrated_from_checkpoint_bytes(
    tmp_path,
) -> None:
    store, binding, task_id = _setup(tmp_path)
    coordinator = _planned(binding)
    live_checkpoint = binding.checkpoint(coordinator)

    assert live_checkpoint.trusted_plan_fingerprint == coordinator.trusted_plan_fingerprint
    host = ProductFactoryCheckpointHost(store)
    saved = host.save(host_task_id=task_id, checkpoint=live_checkpoint)
    loaded = host.load(saved.checkpoint_id)

    assert loaded.checkpoint.trusted_plan_fingerprint is None
    restored = host.restore_latest(host_task_id=task_id, binding=binding)
    assert restored.trusted_plan_fingerprint == coordinator.trusted_plan_fingerprint
    assert restored.snapshot() == coordinator.snapshot()
