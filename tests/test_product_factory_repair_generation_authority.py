from __future__ import annotations

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_checkpoint_host import (
    ProductFactoryCheckpointHost,
    ProductFactoryTrustedPlanAuthorityError,
)
from nika_core.product_factory_coordinator import WorkerResultEnvelope, WorkState
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
from nika_core.toolsmith.contracts import CodingResult, WorkerFailure, WorkerFailureKind

SHA_A = "a" * 40
SHA_B = "b" * 40
DIGEST = "d" * 64
LOCATOR = "org/repo"
PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})


def _setup(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    graph = ProductRepositoryGraph(
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
    project = ProductProjectRepository(store).create(
        project_id="p1",
        name="Product",
        spec=ProductProjectSpec(
            goal="Build durable product",
            desired_outcome="A restart-safe product",
            requirements=(
                ProductRequirement(
                    "req-1",
                    "Repair execution is restart safe",
                    ("New repair generations require host authority",),
                ),
            ),
            repository_refs=(LOCATOR,),
        ),
        idempotency_key="create:p1",
    )
    binding = ProductProjectCoordinatorBinding(project, graph)
    coordinator = binding.plan(
        base_shas={"repo-1": SHA_A},
        component_goals={"core": "build core"},
        permission_ceiling=PERMISSIONS,
    )
    task = TaskQueue(store).create(
        workspace_id="ws-product",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": "p1"},
    )
    return store, binding, coordinator, task.task_id


def _persist_repair_required(store, binding, coordinator, task_id):
    host = ProductFactoryCheckpointHost(store)
    host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))
    request = coordinator.start("core")
    host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))
    coordinator.record_result(
        WorkerResultEnvelope(
            work_id=request.work_id,
            component_id=request.component_id,
            repository_id=request.repository_id,
            base_sha=request.base_sha,
            result_sha=SHA_B,
            diff_digest=DIGEST,
            coding_result=CodingResult(
                job_id=request.work_id,
                failure=WorkerFailure(
                    WorkerFailureKind.PROCESS_FAILED,
                    "worker failed deterministically",
                    retryable=True,
                ),
            ),
        )
    )
    failed = host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))
    assert failed.checkpoint.coordinator.records[0].state is WorkState.REPAIR_REQUIRED
    return host


def test_recomputed_new_repair_generation_needs_live_host_proof_but_new_base_is_allowed(
    tmp_path,
) -> None:
    store, binding, coordinator, task_id = _setup(tmp_path)
    host = _persist_repair_required(store, binding, coordinator, task_id)

    repair = coordinator.prepare_repair(
        "core",
        base_sha=SHA_B,
        reason="retry on independently selected newer base",
    )
    candidate_checkpoint = ProductProjectCoordinatorCheckpoint(
        project_id=binding.project.project_id,
        spec_version=binding.project.spec_version,
        row_version=binding.project.row_version,
        coordinator=coordinator.snapshot(),
    )

    with pytest.raises(
        ProductFactoryTrustedPlanAuthorityError,
        match="new repair generation requires live host authority proof",
    ):
        host.save(host_task_id=task_id, checkpoint=candidate_checkpoint)

    unchanged = host.latest(host_task_id=task_id, project_id="p1")
    assert unchanged is not None
    durable_before = unchanged.checkpoint.coordinator.records[0]
    assert durable_before.request.attempt == 1
    assert durable_before.state is WorkState.REPAIR_REQUIRED

    persisted = host.save(
        host_task_id=task_id,
        checkpoint=binding.checkpoint(coordinator),
    )
    durable_after = persisted.checkpoint.coordinator.records[0]
    assert durable_after.request.work_id == repair.work_id
    assert durable_after.request.attempt == 2
    assert durable_after.request.base_sha == SHA_B
    assert durable_after.state is WorkState.READY
