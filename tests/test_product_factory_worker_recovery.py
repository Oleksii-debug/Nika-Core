import asyncio

import pytest

from nika_core.product_factory_coordinator import (
    CoordinatorError,
    ProductFactoryCoordinator,
    WorkerResultEnvelope,
    WorkState,
)
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from nika_core.product_factory_worker_recovery import (
    ProductFactoryWorkerRecovery,
    WorkerRecoveryDisposition,
)
from nika_core.toolsmith.contracts import (
    CodingResult,
    RecoveryState,
    WorkerFailure,
    WorkerFailureKind,
)
from nika_core.toolsmith.contracts import TestEvidence as WorkerTestEvidence

SHA_A = "a" * 40
SHA_B = "b" * 40
DIGEST = "d" * 64
PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})


def _coordinator() -> ProductFactoryCoordinator:
    graph = ProductRepositoryGraph(
        project_id="project-1",
        repositories=(RepositoryRef("repo-1", "github", "org/repo", "main"),),
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
    coordinator = ProductFactoryCoordinator(graph)
    coordinator.plan(
        base_shas={"repo-1": SHA_A},
        goals={"core": "build core", "ui": "build ui", "docs": "write docs"},
        permission_ceiling=PERMISSIONS,
    )
    return coordinator


def _run(coroutine):
    return asyncio.run(coroutine)


class FakeRecoveryPort:
    def __init__(
        self,
        state: RecoveryState | None,
        *,
        failure: WorkerFailure | None = None,
    ) -> None:
        self.state = state
        self.failure = failure
        self.inspected: list[str] = []
        self.recovered = []

    async def inspect(self, work_id: str) -> RecoveryState | None:
        self.inspected.append(work_id)
        return self.state

    async def recover(self, request, state):
        self.recovered.append((request, state))
        result = CodingResult(
            job_id=request.work_id,
            test_evidence=()
            if self.failure is not None
            else tuple(
                WorkerTestEvidence(command, 0, "tests-ok")
                for command in request.acceptance_commands
            ),
            recovery_state=state,
            failure=self.failure,
        )
        return WorkerResultEnvelope(
            work_id=request.work_id,
            component_id=request.component_id,
            repository_id=request.repository_id,
            base_sha=request.base_sha,
            result_sha=SHA_B,
            diff_digest=DIGEST,
            coding_result=result,
        )


def test_restart_recovery_returns_running_component_to_independent_review() -> None:
    original = _coordinator()
    request = original.start("core")
    snapshot = original.snapshot()
    restored = _coordinator()
    restored.restore(snapshot)
    state = RecoveryState("interrupted", "resume-token")
    worker = FakeRecoveryPort(state)

    outcome = _run(ProductFactoryWorkerRecovery(worker).recover_running(restored, "core"))

    assert outcome.disposition is WorkerRecoveryDisposition.RECOVERED
    assert outcome.record.state is WorkState.REVIEW_REQUIRED
    assert worker.inspected == [request.work_id]
    assert worker.recovered[0][0].work_id == request.work_id
    assert worker.recovered[0][1] == state
    ready = {item.component_id for item in restored.ready_requests()}
    assert "docs" in ready
    assert "ui" not in ready


def test_missing_worker_state_blocks_only_the_lost_component() -> None:
    coordinator = _coordinator()
    coordinator.start("core")
    worker = FakeRecoveryPort(None)

    outcome = _run(ProductFactoryWorkerRecovery(worker).recover_running(coordinator, "core"))

    assert outcome.disposition is WorkerRecoveryDisposition.BLOCKED_MISSING_STATE
    assert outcome.record.state is WorkState.BLOCKED
    assert "host reconciliation required" in (outcome.record.blocker or "")
    assert {item.component_id for item in coordinator.ready_requests()} == {"docs"}


def test_recovered_cancelled_result_preserves_typed_repair_evidence() -> None:
    coordinator = _coordinator()
    coordinator.start("core")
    state = RecoveryState("cancelled", "resume-later")
    failure = WorkerFailure(WorkerFailureKind.CANCELLED, "cancelled before restart")
    worker = FakeRecoveryPort(state, failure=failure)

    outcome = _run(ProductFactoryWorkerRecovery(worker).recover_running(coordinator, "core"))

    assert outcome.record.state is WorkState.REPAIR_REQUIRED
    assert outcome.record.result is not None
    result = outcome.record.result.coding_result
    assert result.failure == failure
    assert result.recovery_state == state


def test_non_running_component_cannot_be_recovered_or_inspected() -> None:
    coordinator = _coordinator()
    worker = FakeRecoveryPort(RecoveryState("running", "token"))

    with pytest.raises(CoordinatorError, match="must be running"):
        _run(ProductFactoryWorkerRecovery(worker).recover_running(coordinator, "docs"))

    assert worker.inspected == []


def test_unknown_component_recovery_fails_closed_without_worker_call() -> None:
    coordinator = _coordinator()
    worker = FakeRecoveryPort(RecoveryState("running", "token"))

    with pytest.raises(CoordinatorError, match="unknown component"):
        _run(ProductFactoryWorkerRecovery(worker).recover_running(coordinator, "missing"))

    assert worker.inspected == []
