import asyncio
from pathlib import Path

import pytest

from nika_core.product_factory_coding_worker_adapter import (
    CodingWorkerAdapterError,
    CodingWorkerComponentAdapter,
    CodingWorkerDispatchContext,
    CodingWorkerExecutionEvidence,
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
    TestEvidence as WorkerTestEvidence,
    WorkerFailure,
    WorkerFailureKind,
    WorkspaceLease,
)

SHA_A = "a" * 40
SHA_B = "b" * 40
DIGEST = "d" * 64
TREE = "tree-v1"
PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})


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


def _context() -> CodingWorkerDispatchContext:
    return CodingWorkerDispatchContext(
        repository_tree_digest=TREE,
        lease=WorkspaceLease(
            lease_id="lease-1",
            workspace_root=Path("worker-root"),
            isolation_class=IsolationClass.PROCESS_CONTAINED,
            expires_at="2026-08-21T00:00:00Z",
        ),
        process_policy=ProcessPolicy(("python",)),
        network_policy=NetworkPolicy(),
        resource_budget=ResourceBudget(300, 1024 * 1024, 20),
    )


def _run(coroutine):
    return asyncio.run(coroutine)


class FakeContexts:
    def __init__(self) -> None:
        self.requests = []

    async def context_for(self, request):
        self.requests.append(request)
        return _context()


class FakeEvidence:
    def __init__(self, *, base_sha=SHA_A) -> None:
        self.base_sha = base_sha
        self.calls = []

    async def collect(self, request, job, result):
        self.calls.append((request, job, result))
        return CodingWorkerExecutionEvidence(
            work_id=request.work_id,
            repository_id=request.repository_id,
            base_sha=self.base_sha,
            result_sha=SHA_B,
            diff_digest=DIGEST,
        )


class FakeWorker:
    def __init__(self, result_factory=None) -> None:
        self.result_factory = result_factory or self._success
        self.executed = []
        self.cancelled = []
        self.inspected = []
        self.recovered = []

    async def execute(self, job):
        self.executed.append(job)
        return self.result_factory(job)

    async def cancel(self, job_id):
        self.cancelled.append(job_id)

    async def inspect(self, job_id):
        self.inspected.append(job_id)
        return RecoveryState("running", "token-1")

    async def recover(self, job, state):
        self.recovered.append((job, state))
        return self.result_factory(job)

    @staticmethod
    def _success(job):
        return CodingResult(
            job_id=job.job_id,
            changed_files=(ChangedFile("src/core/item.py", DIGEST, 10),),
            test_evidence=(WorkerTestEvidence(("python", "-m", "pytest"), 0, "tests-ok"),),
        )


def test_dispatch_maps_component_scope_to_public_coding_job_and_exact_evidence() -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")
    worker = FakeWorker()
    contexts = FakeContexts()
    evidence = FakeEvidence()
    adapter = CodingWorkerComponentAdapter(worker, contexts, evidence)

    envelope = _run(adapter.dispatch(request))

    job = worker.executed[0]
    assert job.job_id == request.work_id
    assert job.task_id == "product:project-1:component:core"
    assert job.repository.repository_id == "repo-1"
    assert job.repository.base_sha == SHA_A
    assert job.repository.tree_digest == TREE
    assert job.allowed_paths.roots == ("src/core",)
    assert job.permission_ceiling == PERMISSIONS
    assert [command.argv for command in job.acceptance_commands] == [
        ("python", "-m", "pytest", "tests/core")
    ]
    assert envelope.result_sha == SHA_B
    assert envelope.diff_digest == DIGEST
    assert envelope.coding_result.test_evidence[0].exit_code == 0


def test_run_component_hands_success_to_independent_review_without_auto_accepting() -> None:
    coordinator = _coordinator()
    adapter = CodingWorkerComponentAdapter(FakeWorker(), FakeContexts(), FakeEvidence())

    record = _run(adapter.run_component(coordinator, "core"))

    assert record.state is WorkState.REVIEW_REQUIRED
    assert "ui" not in {request.component_id for request in coordinator.ready_requests()}
    assert "docs" in {request.component_id for request in coordinator.ready_requests()}


def test_typed_worker_failure_is_preserved_and_coordinator_requires_repair() -> None:
    def fail(job):
        return CodingResult(
            job_id=job.job_id,
            failure=WorkerFailure(WorkerFailureKind.PROCESS_FAILED, "tests failed", retryable=True),
        )

    coordinator = _coordinator()
    adapter = CodingWorkerComponentAdapter(FakeWorker(fail), FakeContexts(), FakeEvidence())

    record = _run(adapter.run_component(coordinator, "core"))

    assert record.state is WorkState.REPAIR_REQUIRED
    assert record.result is not None
    assert record.result.coding_result.failure is not None
    assert record.result.coding_result.failure.kind is WorkerFailureKind.PROCESS_FAILED
    assert [request.component_id for request in coordinator.ready_requests()] == ["docs"]


def test_cancel_and_inspect_delegate_to_same_public_worker_identity() -> None:
    worker = FakeWorker()
    adapter = CodingWorkerComponentAdapter(worker, FakeContexts(), FakeEvidence())

    _run(adapter.cancel("work-1"))
    state = _run(adapter.inspect("work-1"))

    assert worker.cancelled == ["work-1"]
    assert worker.inspected == ["work-1"]
    assert state == RecoveryState("running", "token-1")


def test_recovery_rebuilds_same_bounded_job_and_returns_exact_envelope() -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")
    worker = FakeWorker()
    adapter = CodingWorkerComponentAdapter(worker, FakeContexts(), FakeEvidence())
    state = RecoveryState("interrupted", "resume-token")

    envelope = _run(adapter.recover(request, state))

    recovered_job, recovered_state = worker.recovered[0]
    assert recovered_job.job_id == request.work_id
    assert recovered_job.repository.base_sha == request.base_sha
    assert recovered_job.allowed_paths.roots == request.allowed_paths
    assert recovered_state == state
    assert envelope.work_id == request.work_id
    assert envelope.result_sha == SHA_B


def test_stale_evidence_is_rejected_before_it_can_reach_reconciliation() -> None:
    coordinator = _coordinator()
    request = coordinator.start("core")
    adapter = CodingWorkerComponentAdapter(
        FakeWorker(),
        FakeContexts(),
        FakeEvidence(base_sha="c" * 40),
    )

    with pytest.raises(CodingWorkerAdapterError, match="stale worker evidence"):
        _run(adapter.dispatch(request))


def test_changed_file_outside_component_scope_is_rejected() -> None:
    def outside_scope(job):
        return CodingResult(
            job_id=job.job_id,
            changed_files=(ChangedFile("src/other/item.py", DIGEST, 10),),
            test_evidence=(WorkerTestEvidence(("pytest",), 0, "ok"),),
        )

    coordinator = _coordinator()
    request = coordinator.start("core")
    adapter = CodingWorkerComponentAdapter(
        FakeWorker(outside_scope),
        FakeContexts(),
        FakeEvidence(),
    )

    with pytest.raises(CodingWorkerAdapterError, match="outside component allowed paths"):
        _run(adapter.dispatch(request))


def test_cancelled_result_keeps_typed_cancel_failure() -> None:
    def cancelled(job):
        return CodingResult(
            job_id=job.job_id,
            recovery_state=RecoveryState("cancelled", "resume-later"),
            failure=WorkerFailure(WorkerFailureKind.CANCELLED, "cancelled by coordinator"),
        )

    coordinator = _coordinator()
    adapter = CodingWorkerComponentAdapter(FakeWorker(cancelled), FakeContexts(), FakeEvidence())

    record = _run(adapter.run_component(coordinator, "core"))

    assert record.state is WorkState.REPAIR_REQUIRED
    assert record.result is not None
    assert record.result.coding_result.failure is not None
    assert record.result.coding_result.failure.kind is WorkerFailureKind.CANCELLED
    assert record.result.coding_result.recovery_state == RecoveryState("cancelled", "resume-later")


def test_worker_result_for_wrong_job_id_is_rejected() -> None:
    def wrong_job(_job):
        return CodingResult(
            job_id="foreign-work",
            test_evidence=(WorkerTestEvidence(("pytest",), 0, "ok"),),
        )

    coordinator = _coordinator()
    request = coordinator.start("core")
    adapter = CodingWorkerComponentAdapter(FakeWorker(wrong_job), FakeContexts(), FakeEvidence())

    with pytest.raises(CodingWorkerAdapterError, match="job id"):
        _run(adapter.dispatch(request))
