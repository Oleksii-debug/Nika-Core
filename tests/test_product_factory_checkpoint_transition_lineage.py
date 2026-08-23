from __future__ import annotations

from dataclasses import replace

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
DIGEST_D = "d" * 64
LOCATOR = "org/repo"
PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})


def _spec() -> ProductProjectSpec:
    return ProductProjectSpec(
        goal="Build durable product",
        desired_outcome="A reviewed durable product",
        requirements=(
            ProductRequirement(
                "req-1",
                "Restart-safe orchestration",
                ("Repair attempts survive restart without duplicate execution",),
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
    request = next(
        record.request
        for record in coordinator.snapshot().records
        if record.state is WorkState.RUNNING
    )
    coordinator.record_result(
        WorkerResultEnvelope(
            work_id=request.work_id,
            component_id=request.component_id,
            repository_id=request.repository_id,
            base_sha=request.base_sha,
            result_sha=SHA_B,
            diff_digest=DIGEST_D,
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


def test_repair_requires_prior_durable_repair_required_checkpoint(tmp_path) -> None:
    store, binding, coordinator, task_id = _setup(tmp_path)
    host = ProductFactoryCheckpointHost(store)
    host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))

    coordinator.start("core")
    _fail_running(coordinator)
    coordinator.prepare_repair("core", base_sha=SHA_B, reason="fix failure")

    with pytest.raises(
        ProductFactoryCheckpointIntegrityError,
        match="prior durable repair_required",
    ):
        host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))


def test_attempt_cannot_skip_multiple_durable_repair_generations(tmp_path) -> None:
    store, binding, coordinator, task_id = _setup(tmp_path)
    host = ProductFactoryCheckpointHost(store)
    host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))
    coordinator.start("core")
    host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))
    _fail_running(coordinator)
    host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))

    coordinator.prepare_repair("core", base_sha=SHA_B, reason="repair one")
    coordinator.start("core")
    _fail_running(coordinator)
    coordinator.prepare_repair("core", base_sha=SHA_A, reason="repair two")

    with pytest.raises(
        ProductFactoryCheckpointIntegrityError,
        match="attempt skipped or regressed",
    ):
        host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))


def test_same_attempt_state_cannot_roll_back_with_recomputed_candidate_snapshot(tmp_path) -> None:
    store, binding, coordinator, task_id = _setup(tmp_path)
    host = ProductFactoryCheckpointHost(store)
    host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))
    coordinator.start("core")
    running_checkpoint = binding.checkpoint(coordinator)
    host.save(host_task_id=task_id, checkpoint=running_checkpoint)

    running_snapshot = running_checkpoint.coordinator
    running_record = running_snapshot.records[0]
    forged_snapshot = replace(
        running_snapshot,
        revision=running_snapshot.revision + 1,
        records=(replace(running_record, state=WorkState.READY),),
    )
    forged_checkpoint = ProductProjectCoordinatorCheckpoint(
        project_id=binding.project.project_id,
        spec_version=binding.project.spec_version,
        row_version=binding.project.row_version,
        coordinator=forged_snapshot,
    )

    with pytest.raises(
        ProductFactoryCheckpointIntegrityError,
        match="state regressed or bypassed",
    ):
        host.save(host_task_id=task_id, checkpoint=forged_checkpoint)


def test_legitimate_repair_lineage_remains_restart_resumable(tmp_path) -> None:
    store, binding, coordinator, task_id = _setup(tmp_path)
    host = ProductFactoryCheckpointHost(store)
    host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))

    coordinator.start("core")
    host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))
    _fail_running(coordinator)
    host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))
    repair = coordinator.prepare_repair("core", base_sha=SHA_B, reason="fix failure")
    saved = host.save(host_task_id=task_id, checkpoint=binding.checkpoint(coordinator))

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted_project = ProductProjectRepository(restarted_store).get("p1")
    restarted_binding = ProductProjectCoordinatorBinding(restarted_project, _graph())
    restored = ProductFactoryCheckpointHost(restarted_store).restore_latest(
        host_task_id=task_id,
        binding=restarted_binding,
    )
    record = restored.snapshot().records[0]

    assert saved.checkpoint.coordinator.records[0].request.work_id == repair.work_id
    assert record.request.work_id == repair.work_id
    assert record.request.attempt == 2
    assert record.state is WorkState.READY
