from __future__ import annotations

from dataclasses import replace
from typing import cast

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_checkpoint_host import ProductFactoryCheckpointHost
from nika_core.product_factory_coding_worker_adapter import CodingWorkerComponentAdapter
from nika_core.product_factory_coordinator import ReviewDecision, WorkerResultEnvelope
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from nika_core.product_factory_project_binding import ProductProjectCoordinatorBinding
from nika_core.product_factory_toolsmith_integration import (
    ProductFactoryToolsmithBridge,
    ProductFactoryToolsmithError,
)
from nika_core.product_project import (
    ProductProjectRepository,
    ProductProjectSpec,
    ProductRequirement,
)
from nika_core.toolsmith.contracts import (
    CandidateState,
    CodingResult,
    TestEvidence,
    WorkerFailure,
    WorkerFailureKind,
)

SHA_A = "a" * 40
SHA_B = "b" * 40
DIFF_DIGEST = "d" * 64
TEST_DIGEST = "e" * 64
PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})


class RecordingEscalation:
    def __init__(self) -> None:
        self.begun = []

    def begin(self, gap):
        self.begun.append(gap)
        return 0, CandidateState.PROPOSED

    def reconcile_resume(self, *, task_id, capability_id):
        return None


def _graph() -> ProductRepositoryGraph:
    return ProductRepositoryGraph(
        project_id="project-1",
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


def _spec() -> ProductProjectSpec:
    return ProductProjectSpec(
        goal="Build a durable product",
        desired_outcome="Reviewed component",
        requirements=(
            ProductRequirement(
                "req-1",
                "Only a durable failed worker attempt may escalate a capability gap",
                ("caller-created work cannot become Toolsmith authority",),
            ),
        ),
        repository_refs=("org/repo",),
    )


def _setup(tmp_path, *, persist: bool = True, review_rejection: bool = False):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    project = ProductProjectRepository(store).create(
        project_id="project-1",
        name="Toolsmith authority product",
        spec=_spec(),
        idempotency_key="create:project-1",
    )
    graph = _graph()
    binding = ProductProjectCoordinatorBinding(project, graph)
    coordinator = binding.plan(
        base_shas={"repo-1": SHA_A},
        component_goals={"core": "Implement core"},
        permission_ceiling=PERMISSIONS,
    )
    request = coordinator.start("core")
    if review_rejection:
        coding_result = CodingResult(
            job_id=request.work_id,
            test_evidence=(
                TestEvidence(
                    command=request.acceptance_commands[0],
                    exit_code=0,
                    output_digest=TEST_DIGEST,
                ),
            ),
        )
    else:
        coding_result = CodingResult(
            job_id=request.work_id,
            failure=WorkerFailure(
                WorkerFailureKind.PROCESS_FAILED,
                "missing safe TOML capability",
                retryable=True,
            ),
        )
    coordinator.record_result(
        WorkerResultEnvelope(
            work_id=request.work_id,
            component_id=request.component_id,
            repository_id=request.repository_id,
            base_sha=request.base_sha,
            result_sha=SHA_B,
            diff_digest=DIFF_DIGEST,
            coding_result=coding_result,
        )
    )
    if review_rejection:
        coordinator.review(
            "core",
            ReviewDecision(
                reviewer_id="independent-qa",
                accepted=False,
                reason="review-directed change required",
                evidence_refs=("qa:review-1",),
            ),
        )
    host_task = TaskQueue(store).create(
        workspace_id="ws-product",
        agent_id="product-factory",
        payload={
            "kind": "product_factory",
            "product_project_id": project.project_id,
        },
    )
    if persist:
        ProductFactoryCheckpointHost(store).save(
            host_task_id=host_task.task_id,
            checkpoint=binding.checkpoint(coordinator),
        )
    escalation = RecordingEscalation()
    bridge = ProductFactoryToolsmithBridge(
        escalation,
        cast(CodingWorkerComponentAdapter, object()),
        store=store,
    )
    return store, binding, coordinator, request, host_task.task_id, escalation, bridge


def _binding_count(store: SQLiteStore) -> int:
    with store.connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS count FROM product_factory_toolsmith_bindings"
        ).fetchone()
    return int(row["count"])


def test_forged_request_is_rejected_before_binding_or_toolsmith_effect(tmp_path) -> None:
    store, _, _, request, task_id, escalation, bridge = _setup(tmp_path)
    forged = replace(
        request,
        permission_ceiling=request.permission_ceiling | frozenset({"deploy"}),
    )

    with pytest.raises(ProductFactoryToolsmithError, match="durable failed attempt"):
        bridge.begin_durable_gap(
            forged,
            host_task_id=task_id,
            capability_id="toml-editor",
            reason="caller claims a capability gap",
        )

    assert escalation.begun == []
    assert _binding_count(store) == 0


def test_unpersisted_failed_request_cannot_start_toolsmith_effect(tmp_path) -> None:
    store, _, _, request, task_id, escalation, bridge = _setup(
        tmp_path,
        persist=False,
    )

    with pytest.raises(ProductFactoryToolsmithError, match="durable Product Factory checkpoint"):
        bridge.begin_durable_gap(
            request,
            host_task_id=task_id,
            capability_id="toml-editor",
            reason="missing capability",
        )

    assert escalation.begun == []
    assert _binding_count(store) == 0


def test_stale_failed_attempt_cannot_start_gap_after_new_attempt_is_durable(tmp_path) -> None:
    store, binding, coordinator, request, task_id, escalation, bridge = _setup(tmp_path)
    next_request = coordinator.prepare_repair(
        "core",
        base_sha=SHA_B,
        reason="independent repair path",
    )
    assert next_request.attempt == 2
    ProductFactoryCheckpointHost(store).save(
        host_task_id=task_id,
        checkpoint=binding.checkpoint(coordinator),
    )

    with pytest.raises(ProductFactoryToolsmithError, match="durable failed attempt"):
        bridge.begin_durable_gap(
            request,
            host_task_id=task_id,
            capability_id="toml-editor",
            reason="stale capability gap",
        )

    assert escalation.begun == []
    assert _binding_count(store) == 0


def test_review_rejection_is_not_laundered_into_worker_capability_gap(tmp_path) -> None:
    store, _, _, request, task_id, escalation, bridge = _setup(
        tmp_path,
        review_rejection=True,
    )

    with pytest.raises(ProductFactoryToolsmithError, match="worker-failure evidence"):
        bridge.begin_durable_gap(
            request,
            host_task_id=task_id,
            capability_id="toml-editor",
            reason="review rejection is not a worker capability failure",
        )

    assert escalation.begun == []
    assert _binding_count(store) == 0
