from __future__ import annotations

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_checkpoint_host import ProductFactoryCheckpointHost
from nika_core.product_factory_coordinator import ReviewDecision, WorkerResultEnvelope
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
from nika_core.toolsmith.contracts import CodingResult, TestEvidence

SHA_A = "a" * 40
SHA_B = "b" * 40
DIFF_DIGEST = "d" * 64
TEST_DIGEST = "e" * 64
PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})
ACCEPTANCE_COMMAND = ("python", "-m", "pytest", "tests/core")


def _setup(tmp_path):
    store = SQLiteStore(tmp_path / "nika50-pf12-review.db")
    store.initialize()
    project = ProductProjectRepository(store).create(
        project_id="nika50-review-project",
        name="NIKA50 review evidence",
        spec=ProductProjectSpec(
            goal="Build a safe product",
            desired_outcome="Keep reviewer credentials out of durable checkpoints",
            requirements=(
                ProductRequirement(
                    "req-review-secret",
                    "review evidence is secret-free",
                    ("no credential material in checkpoint bytes",),
                ),
            ),
            repository_refs=("org/repo",),
        ),
        idempotency_key="nika50:review-secret:create",
    )
    graph = ProductRepositoryGraph(
        project_id=project.project_id,
        repositories=(RepositoryRef("repo-1", "github", "org/repo", "main"),),
        components=(
            ProductComponent(
                component_id="core",
                repository_id="repo-1",
                paths=("src/core",),
                test_commands=(ACCEPTANCE_COMMAND,),
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
        workspace_id="nika50-review-workspace",
        agent_id="product-factory",
        payload={
            "kind": "product_factory",
            "product_project_id": project.project_id,
        },
    )
    return store, binding, coordinator, task.task_id


def _record_success(coordinator) -> None:
    request = coordinator.start("core")
    coordinator.record_result(
        WorkerResultEnvelope(
            work_id=request.work_id,
            component_id=request.component_id,
            repository_id=request.repository_id,
            base_sha=request.base_sha,
            result_sha=SHA_B,
            diff_digest=DIFF_DIGEST,
            coding_result=CodingResult(
                job_id=request.work_id,
                test_evidence=(
                    TestEvidence(
                        command=ACCEPTANCE_COMMAND,
                        exit_code=0,
                        output_digest=TEST_DIGEST,
                    ),
                ),
            ),
        )
    )


def test_review_reason_and_evidence_refs_never_reach_durable_checkpoint(tmp_path) -> None:
    reason_canary = "NIKA50_REVIEW_BEARER_CANARY_42A7"
    evidence_canary = "NIKA50_REVIEW_REF_CANARY_91D3"
    store, binding, coordinator, task_id = _setup(tmp_path)
    _record_success(coordinator)

    coordinator.review(
        "core",
        ReviewDecision(
            reviewer_id="independent-qa",
            accepted=True,
            reason="verified; Authorization: Bearer " + reason_canary,
            evidence_refs=(
                "https://reviewer:" + evidence_canary + "@example.invalid/evidence",
            ),
        ),
    )

    ProductFactoryCheckpointHost(store).save(
        host_task_id=task_id,
        checkpoint=binding.checkpoint(coordinator),
    )

    with store.connection() as conn:
        row = conn.execute(
            "SELECT payload_json FROM checkpoints WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    assert row is not None
    raw = str(row[0])

    assert reason_canary not in raw
    assert evidence_canary not in raw
