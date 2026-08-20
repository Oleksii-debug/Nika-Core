import pytest

from nika_core.product_factory_coordinator import (
    CoordinatorError,
    ProductFactoryCoordinator,
    ReviewDecision,
    WorkerResultEnvelope,
    WorkState,
)
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from nika_core.toolsmith.contracts import (
    CodingResult,
    TestEvidence,
    WorkerFailure,
    WorkerFailureKind,
)


SHA_A = "a" * 40
SHA_B = "b" * 40
DIGEST = "d" * 64
PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})


def _graph() -> ProductRepositoryGraph:
    return ProductRepositoryGraph(
        project_id="project-1",
        repositories=(
            RepositoryRef("repo-1", "github", "org/repo", "main"),
        ),
        components=(
            ProductComponent(
                component_id="core",
                repository_id="repo-1",
                paths=("src/core",),
                test_commands=(("python", "-m", "pytest", "tests/core"),),
            ),
            ProductComponent(
                component_id="ui",
                repository_id="repo-1",
                paths=("src/ui",),
                dependencies=("core",),
                test_commands=(("python", "-m", "pytest", "tests/ui"),),
            ),
            ProductComponent(
                component_id="docs",
                repository_id="repo-1",
                paths=("docs/product",),
                test_commands=(("python", "-m", "pytest", "tests/docs"),),
            ),
        ),
    )


def _coordinator() -> ProductFactoryCoordinator:
    coordinator = ProductFactoryCoordinator(_graph())
    coordinator.plan(
        base_shas={"repo-1": SHA_A},
        goals={"core": "build core", "ui": "build ui", "docs": "write docs"},
        permission_ceiling=PERMISSIONS,
    )
    return coordinator


def _success(request, *, base_sha=SHA_A) -> WorkerResultEnvelope:
    return WorkerResultEnvelope(
        work_id=request.work_id,
        component_id=request.component_id,
        repository_id=request.repository_id,
        base_sha=base_sha,
        result_sha=SHA_B,
        diff_digest=DIGEST,
        coding_result=CodingResult(
            job_id=request.work_id,
            test_evidence=(TestEvidence(("pytest",), 0, "ok"),),
        ),
    )


def test_dependency_ready_scheduling_keeps_independent_component_parallel() -> None:
    coordinator = _coordinator()
    assert [item.component_id for item in coordinator.ready_requests()] == ["core", "docs"]


def test_dependency_unblocks_only_after_independent_review_accepts() -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")
    coordinator.record_result(_success(request))
    assert "ui" not in {item.component_id for item in coordinator.ready_requests()}
    coordinator.review("core", ReviewDecision("qa-1", True, "verified", ("ci:1",)))
    assert "ui" in {item.component_id for item in coordinator.ready_requests()}


def test_stale_worker_result_is_rejected() -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")
    with pytest.raises(CoordinatorError, match="stale worker result"):
        coordinator.record_result(_success(request, base_sha="c" * 40))


def test_success_without_test_evidence_is_rejected() -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")
    envelope = WorkerResultEnvelope(
        request.work_id,
        request.component_id,
        request.repository_id,
        request.base_sha,
        SHA_B,
        DIGEST,
        CodingResult(job_id=request.work_id),
    )
    with pytest.raises(CoordinatorError, match="passing test evidence"):
        coordinator.record_result(envelope)


def test_worker_failure_transitions_to_repair_and_new_attempt_is_deterministic() -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")
    envelope = WorkerResultEnvelope(
        request.work_id,
        request.component_id,
        request.repository_id,
        request.base_sha,
        SHA_B,
        DIGEST,
        CodingResult(
            job_id=request.work_id,
            failure=WorkerFailure(WorkerFailureKind.PROCESS_FAILED, "tests failed", retryable=True),
        ),
    )
    record = coordinator.record_result(envelope)
    assert record.state is WorkState.REPAIR_REQUIRED
    repair = coordinator.prepare_repair("core", base_sha=SHA_A, reason="fix tests")
    assert repair.attempt == 2
    assert repair.work_id != request.work_id


def test_rejected_review_requires_repair() -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")
    coordinator.record_result(_success(request))
    record = coordinator.review("core", ReviewDecision("qa-1", False, "unsafe diff", ("review:2",)))
    assert record.state is WorkState.REPAIR_REQUIRED
    assert record.blocker == "unsafe diff"


def test_blocked_component_does_not_block_independent_component() -> None:
    coordinator = _coordinator()
    coordinator.block("core", "external dependency")
    assert [item.component_id for item in coordinator.ready_requests()] == ["docs"]


def test_snapshot_restore_preserves_state_and_ready_set() -> None:
    coordinator = _coordinator()
    coordinator.start("docs")
    snapshot = coordinator.snapshot()
    restored = ProductFactoryCoordinator(_graph())
    restored.restore(snapshot)
    assert restored.snapshot() == snapshot
    assert [item.component_id for item in restored.ready_requests()] == ["core"]


def test_restore_rejects_foreign_project() -> None:
    coordinator = _coordinator()
    snapshot = coordinator.snapshot()
    foreign_graph = ProductRepositoryGraph(
        project_id="project-2",
        repositories=_graph().repositories,
        components=_graph().components,
    )
    with pytest.raises(CoordinatorError, match="snapshot project"):
        ProductFactoryCoordinator(foreign_graph).restore(snapshot)


def test_plan_rejects_missing_component_goal() -> None:
    coordinator = ProductFactoryCoordinator(_graph())
    with pytest.raises(CoordinatorError, match="missing base SHA or goal"):
        coordinator.plan(
            base_shas={"repo-1": SHA_A},
            goals={"core": "build", "ui": "build"},
            permission_ceiling=PERMISSIONS,
        )
