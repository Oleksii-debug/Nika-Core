from __future__ import annotations

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_checkpoint_host import (
    ProductFactoryCheckpointHost,
    ProductFactoryCheckpointIntegrityError,
)
from nika_core.product_factory_coordinator import (
    WorkerResultEnvelope,
    WorkState,
)
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from nika_core.product_factory_program_host import ProductFactoryProgramHost
from nika_core.product_factory_project_binding import ProductProjectCoordinatorBinding
from nika_core.product_project import (
    ProductProjectRepository,
    ProductProjectSpec,
    ProductRequirement,
)
from nika_core.toolsmith.contracts import (
    CodingResult,
    WorkerFailure,
    WorkerFailureKind,
)

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
                    ("New repair attempts are durable before worker execution",),
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


def _fail_running(coordinator) -> None:
    request = coordinator.snapshot().records[0].request
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


def _persist_repair_required(store, binding, coordinator, task_id):
    host = ProductFactoryCheckpointHost(store)
    host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))
    coordinator.start("core")
    host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))
    _fail_running(coordinator)
    failed = host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))
    assert failed.checkpoint.coordinator.records[0].state is WorkState.REPAIR_REQUIRED
    return host


def test_new_repair_attempt_cannot_first_be_persisted_after_execution_started(tmp_path) -> None:
    store, binding, coordinator, task_id = _setup(tmp_path)
    host = _persist_repair_required(store, binding, coordinator, task_id)

    coordinator.prepare_repair("core", base_sha=SHA_B, reason="repair safely")
    coordinator.start("core")

    with pytest.raises(
        ProductFactoryCheckpointIntegrityError,
        match="durably checkpointed as ready before execution",
    ):
        host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))

    latest = host.latest(host_task_id=task_id, project_id="p1")
    assert latest is not None
    durable = latest.checkpoint.coordinator.records[0]
    assert durable.request.attempt == 1
    assert durable.state is WorkState.REPAIR_REQUIRED


def test_program_host_persists_new_repair_attempt_ready_before_dispatch(tmp_path) -> None:
    store, binding, coordinator, task_id = _setup(tmp_path)
    _persist_repair_required(store, binding, coordinator, task_id)
    program_host = ProductFactoryProgramHost(store, worker=object())

    repair = program_host.prepare_repair_and_checkpoint(
        host_task_id=task_id,
        binding=binding,
        coordinator=coordinator,
        component_id="core",
        base_sha=SHA_B,
        reason="repair safely",
    )

    latest = ProductFactoryCheckpointHost(store).latest(
        host_task_id=task_id,
        project_id="p1",
    )
    assert latest is not None
    durable = latest.checkpoint.coordinator.records[0]
    assert durable.request.work_id == repair.work_id
    assert durable.request.attempt == 2
    assert durable.state is WorkState.READY
