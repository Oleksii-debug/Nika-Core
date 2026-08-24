from __future__ import annotations

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_checkpoint_host import ProductFactoryCheckpointHost
from nika_core.product_factory_coordinator import WorkerResultEnvelope
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
from nika_core.toolsmith.contracts import CodingResult, RecoveryState, WorkerFailure, WorkerFailureKind

SHA_A = "a" * 40
SHA_B = "b" * 40
DIGEST = "d" * 64
PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})


def _setup(tmp_path):
    store = SQLiteStore(tmp_path / "qa53-pf12.db")
    store.initialize()
    project = ProductProjectRepository(store).create(
        project_id="qa53-project",
        name="QA53",
        spec=ProductProjectSpec(
            goal="Build safe product",
            desired_outcome="No durable credential leakage",
            requirements=(
                ProductRequirement("req-1", "safe", ("secret-free checkpoints",)),
            ),
            repository_refs=("org/repo",),
        ),
        idempotency_key="qa53:create",
    )
    graph = ProductRepositoryGraph(
        project_id="qa53-project",
        repositories=(RepositoryRef("repo-1", "github", "org/repo", "main"),),
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
        base_shas={"repo-1": SHA_A},
        component_goals={"core": "build core"},
        permission_ceiling=PERMISSIONS,
    )
    task = TaskQueue(store).create(
        workspace_id="qa53-workspace",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": "qa53-project"},
    )
    return store, binding, coordinator, task.task_id


def test_qa53_worker_failure_and_recovery_tokens_never_reach_durable_checkpoint(tmp_path) -> None:
    failure_canary = "QA53_CANARY_CHECKPOINT_FAILURE_42A7"
    recovery_canary = "QA53_CANARY_CHECKPOINT_RECOVERY_91D3"
    store, binding, coordinator, task_id = _setup(tmp_path)
    request = coordinator.start("core")
    result = CodingResult(
        job_id=request.work_id,
        recovery_state=RecoveryState(
            phase="failed",
            opaque_token="access_token=" + recovery_canary,
        ),
        failure=WorkerFailure(
            kind=WorkerFailureKind.PROCESS_FAILED,
            message="subprocess failed with api_key=" + failure_canary,
            retryable=True,
        ),
    )
    coordinator.record_result(
        WorkerResultEnvelope(
            work_id=request.work_id,
            component_id="core",
            repository_id="repo-1",
            base_sha=SHA_A,
            result_sha=SHA_B,
            diff_digest=DIGEST,
            coding_result=result,
        )
    )

    ProductFactoryCheckpointHost(store).save(
        host_task_id=task_id,
        checkpoint=binding.checkpoint(coordinator),
    )

    with store.connection() as conn:
        raw = str(
            conn.execute(
                "SELECT payload_json FROM checkpoints WHERE task_id = ?",
                (task_id,),
            ).fetchone()[0]
        )

    assert failure_canary not in raw
    assert recovery_canary not in raw
