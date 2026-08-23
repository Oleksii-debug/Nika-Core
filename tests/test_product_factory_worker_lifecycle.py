import asyncio
from pathlib import Path

import pytest

from nika_core.product_factory_coding_worker_adapter import (
    CodingWorkerAdapterError,
    CodingWorkerComponentAdapter,
    CodingWorkerDispatchContext,
    CodingWorkerExecutionEvidence,
    ComponentWorkerDisposition,
)
from nika_core.product_factory_coordinator import ProductFactoryCoordinator, WorkState
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from nika_core.toolsmith.contracts import (
    ChangedFile,
    CodingResult,
    IsolationClass,
    NetworkPolicy,
    ProcessPolicy,
    RecoveryState,
    ResourceBudget,
    TestEvidence,
    WorkerFailure,
    WorkerFailureKind,
    WorkspaceLease,
)

SHA_A = "a" * 40
SHA_B = "b" * 40
DIGEST = "d" * 64
PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})


def _run(coroutine):
    return asyncio.run(coroutine)


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
        ),
    )
    coordinator = ProductFactoryCoordinator(graph)
    coordinator.plan(
        base_shas={"repo-1": SHA_A},
        goals={"core": "build core"},
        permission_ceiling=PERMISSIONS,
    )
    return coordinator


class Contexts:
    async def context_for(self, _request):
        return CodingWorkerDispatchContext(
            repository_tree_digest="tree-v1",
            lease=WorkspaceLease(
                lease_id="lease-1",
                workspace_root=Path("worker-root"),
                isolation_class=IsolationClass.PROCESS_CONTAINED,
                expires_at="2026-08-24T00:00:00Z",
            ),
            process_policy=ProcessPolicy(("python",)),
            network_policy=NetworkPolicy(),
            resource_budget=ResourceBudget(300, 1_000_000, 2),
        )


class Evidence:
    async def collect(self, request, _job, _result):
        return CodingWorkerExecutionEvidence(
            work_id=request.work_id,
            repository_id=request.repository_id,
            base_sha=request.base_sha,
            result_sha=SHA_B,
            diff_digest=DIGEST,
        )


class Worker:
    def __init__(self, execute_result, *, inspect_state=RecoveryState("running", "token")):
        self.execute_result = execute_result
        self.inspect_state = inspect_state
        self.cancelled = []
        self.recovered = []

    async def execute(self, job):
        return self.execute_result(job)

    async def cancel(self, job_id):
        self.cancelled.append(job_id)

    async def inspect(self, _job_id):
        return self.inspect_state

    async def recover(self, job, state):
        self.recovered.append((job, state))
        return self.execute_result(job)


def _success(job):
    return CodingResult(
        job_id=job.job_id,
        changed_files=(ChangedFile("src/core/item.py", DIGEST, 10),),
        test_evidence=(
            TestEvidence(("python", "-m", "pytest", "tests/core"), 0, "tests-ok"),
        ),
    )


def _failure(kind, *, retryable):
    def factory(job):
        return CodingResult(
            job_id=job.job_id,
            failure=WorkerFailure(kind, f"{kind.value} failure", retryable=retryable),
        )

    return factory


def test_retryable_process_failure_is_classified_without_auto_retry() -> None:
    coordinator = _coordinator()
    adapter = CodingWorkerComponentAdapter(
        Worker(_failure(WorkerFailureKind.PROCESS_FAILED, retryable=True)),
        Contexts(),
        Evidence(),
    )

    outcome = _run(adapter.run_component_outcome(coordinator, "core"))

    assert outcome.disposition is ComponentWorkerDisposition.RETRYABLE_FAILURE
    assert outcome.record.state is WorkState.REPAIR_REQUIRED
    assert outcome.failure is not None
    assert outcome.failure.kind is WorkerFailureKind.PROCESS_FAILED
    assert coordinator.ready_requests() == ()


def test_policy_failure_is_terminal_even_if_worker_marks_it_retryable() -> None:
    coordinator = _coordinator()
    adapter = CodingWorkerComponentAdapter(
        Worker(_failure(WorkerFailureKind.POLICY_VIOLATION, retryable=True)),
        Contexts(),
        Evidence(),
    )

    outcome = _run(adapter.run_component_outcome(coordinator, "core"))

    assert outcome.disposition is ComponentWorkerDisposition.TERMINAL_FAILURE
    with pytest.raises(CodingWorkerAdapterError, match="not eligible for automatic repair"):
        adapter.prepare_safe_repair(coordinator, "core", reason="do not bypass policy")


def test_safe_repair_uses_exact_failed_result_sha_and_preserves_component_scope() -> None:
    coordinator = _coordinator()
    adapter = CodingWorkerComponentAdapter(
        Worker(_failure(WorkerFailureKind.TIMEOUT, retryable=True)),
        Contexts(),
        Evidence(),
    )
    first = _run(adapter.run_component_outcome(coordinator, "core"))

    repair = adapter.prepare_safe_repair(coordinator, "core", reason="split bounded work")

    assert first.record.result is not None
    assert repair.attempt == 2
    assert repair.base_sha == first.record.result.result_sha == SHA_B
    assert repair.allowed_paths == first.record.request.allowed_paths
    assert repair.permission_ceiling == first.record.request.permission_ceiling
    assert repair.acceptance_commands == first.record.request.acceptance_commands
    assert repair.work_id != first.work_id


def test_cancel_requires_recoverable_post_cancel_state_or_blocks_fail_closed() -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")
    worker = Worker(_success, inspect_state=None)
    adapter = CodingWorkerComponentAdapter(worker, Contexts(), Evidence())

    outcome = _run(adapter.cancel_component(coordinator, "core"))

    assert worker.cancelled == [request.work_id]
    assert outcome.disposition is ComponentWorkerDisposition.CANCEL_RECOVERY_UNAVAILABLE
    assert outcome.record.state is WorkState.BLOCKED
    assert "host reconciliation required" in (outcome.record.blocker or "")
    assert worker.recovered == []


def test_cancel_reconciles_typed_cancel_result_before_product_state_changes() -> None:
    def cancelled(job):
        return CodingResult(
            job_id=job.job_id,
            recovery_state=RecoveryState("cancelled", "cancel-token"),
            failure=WorkerFailure(WorkerFailureKind.CANCELLED, "cancelled by owner"),
        )

    coordinator = _coordinator()
    request = coordinator.start("core")
    worker = Worker(cancelled, inspect_state=RecoveryState("cancelling", "cancel-token"))
    adapter = CodingWorkerComponentAdapter(worker, Contexts(), Evidence())

    outcome = _run(adapter.cancel_component(coordinator, "core"))

    assert worker.cancelled == [request.work_id]
    assert outcome.disposition is ComponentWorkerDisposition.CANCELLED
    assert outcome.record.state is WorkState.REPAIR_REQUIRED
    assert outcome.failure is not None
    assert outcome.failure.kind is WorkerFailureKind.CANCELLED
    assert outcome.recovery_state == RecoveryState("cancelling", "cancel-token")
    with pytest.raises(CodingWorkerAdapterError, match="not eligible for automatic repair"):
        adapter.prepare_safe_repair(coordinator, "core", reason="should stay cancelled")


def test_cancel_race_that_already_completed_still_requires_independent_review() -> None:
    coordinator = _coordinator()
    coordinator.start("core")
    adapter = CodingWorkerComponentAdapter(Worker(_success), Contexts(), Evidence())

    outcome = _run(adapter.cancel_component(coordinator, "core"))

    assert outcome.disposition is ComponentWorkerDisposition.REVIEW_REQUIRED
    assert outcome.record.state is WorkState.REVIEW_REQUIRED


def test_case_variant_duplicate_changed_file_identity_is_rejected() -> None:
    def duplicate(job):
        return CodingResult(
            job_id=job.job_id,
            changed_files=(
                ChangedFile("src/core/Item.py", DIGEST, 10),
                ChangedFile("src\\core\\item.py", "e" * 64, 11),
            ),
            test_evidence=(
                TestEvidence(("python", "-m", "pytest", "tests/core"), 0, "ok"),
            ),
        )

    coordinator = _coordinator()
    request = coordinator.start("core")
    adapter = CodingWorkerComponentAdapter(Worker(duplicate), Contexts(), Evidence())

    with pytest.raises(CodingWorkerAdapterError, match="repeats changed-file identity"):
        _run(adapter.dispatch(request))


def test_changed_file_budget_is_enforced_before_evidence_collection() -> None:
    def too_many(job):
        return CodingResult(
            job_id=job.job_id,
            changed_files=(
                ChangedFile("src/core/a.py", DIGEST, 1),
                ChangedFile("src/core/b.py", "e" * 64, 1),
                ChangedFile("src/core/c.py", "f" * 64, 1),
            ),
        )

    coordinator = _coordinator()
    request = coordinator.start("core")
    adapter = CodingWorkerComponentAdapter(Worker(too_many), Contexts(), Evidence())

    with pytest.raises(CodingWorkerAdapterError, match="changed-file budget"):
        _run(adapter.dispatch(request))