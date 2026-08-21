from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_checkpoint_host import ProductFactoryCheckpointHost
from nika_core.product_factory_coding_worker_adapter import (
    CodingWorkerComponentAdapter,
    CodingWorkerDispatchContext,
    CodingWorkerExecutionEvidence,
)
from nika_core.product_factory_coordinator import (
    ReviewDecision,
    WorkerResultEnvelope,
    WorkState,
)
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from nika_core.product_factory_program_host import (
    ProductFactoryProgramError,
    ProductFactoryProgramHost,
    ProgramWorkDisposition,
)
from nika_core.product_factory_project_binding import ProductProjectCoordinatorBinding
from nika_core.product_project import (
    ProductProjectRepository,
    ProductProjectSpec,
    ProductRequirement,
)
from nika_core.runtime.idempotency import (
    IdempotencyLedger,
    IdempotencyStatus,
)
from nika_core.toolsmith.contracts import (
    CodingResult,
    IsolationClass,
    NetworkPolicy,
    ProcessPolicy,
    RecoveryState,
    ResourceBudget,
    WorkerFailure,
    WorkerFailureKind,
    WorkspaceLease,
)
from nika_core.toolsmith.contracts import (
    TestEvidence as WorkerTestEvidence,
)

PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})
LOCATOR = "org/program-host"
SHA_A = "a" * 40
SHA_B = "b" * 40
DIGEST = "d" * 64


def _sha(index: int) -> str:
    return f"{index:040x}"[-40:]


def _digest(index: int) -> str:
    return f"{index:064x}"[-64:]


def _graph(component_count: int = 3, repository_count: int = 1) -> ProductRepositoryGraph:
    repositories = tuple(
        RepositoryRef(
            repository_id=f"repo-{index}",
            provider="github",
            locator=f"{LOCATOR}-{index}",
            default_branch="main",
        )
        for index in range(repository_count)
    )
    components = []
    for index in range(component_count):
        repository_index = index % repository_count
        dependencies = ()
        if component_count == 3 and index == 1:
            dependencies = ("component-0",)
        components.append(
            ProductComponent(
                component_id=f"component-{index}",
                repository_id=f"repo-{repository_index}",
                paths=(f"src/component-{index}",),
                dependencies=dependencies,
                test_commands=(("python", "-m", "pytest", f"tests/component-{index}"),),
            )
        )
    return ProductRepositoryGraph(
        project_id="project-1",
        repositories=repositories,
        components=tuple(components),
    )


def _spec(graph: ProductRepositoryGraph, goal: str = "Build the product") -> ProductProjectSpec:
    return ProductProjectSpec(
        goal=goal,
        desired_outcome="Reviewed bounded components",
        requirements=(
            ProductRequirement(
                "req-1",
                "Every component must have exact worker and review evidence",
                ("All components pass deterministic acceptance checks",),
            ),
        ),
        repository_refs=tuple(repository.locator for repository in graph.repositories),
    )


def _setup(tmp_path, *, component_count: int = 3, repository_count: int = 1):
    graph = _graph(component_count, repository_count)
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    projects = ProductProjectRepository(store)
    project = projects.create(
        project_id="project-1",
        name="Program Host Product",
        spec=_spec(graph),
        idempotency_key="create:project-1",
    )
    binding = ProductProjectCoordinatorBinding(project, graph)
    task = TaskQueue(store).create(
        workspace_id="ws-product",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": project.project_id},
    )
    coordinator = binding.plan(
        base_shas={
            repository.repository_id: _sha(index + 1)
            for index, repository in enumerate(graph.repositories)
        },
        component_goals={
            component.component_id: f"Implement {component.component_id}"
            for component in graph.components
        },
        permission_ceiling=PERMISSIONS,
    )
    ProductFactoryCheckpointHost(store).save(
        host_task_id=task.task_id,
        checkpoint=binding.checkpoint(coordinator),
    )
    return store, projects, binding, task.task_id, coordinator, graph


def _envelope(request, ordinal: int = 1, *, failure: WorkerFailure | None = None):
    result = CodingResult(
        job_id=request.work_id,
        test_evidence=(
            ()
            if failure is not None
            else (
                WorkerTestEvidence(
                    ("python", "-m", "pytest", request.component_id),
                    0,
                    _digest(ordinal),
                ),
            )
        ),
        failure=failure,
    )
    return WorkerResultEnvelope(
        work_id=request.work_id,
        component_id=request.component_id,
        repository_id=request.repository_id,
        base_sha=request.base_sha,
        result_sha=_sha(10_000 + ordinal),
        diff_digest=_digest(20_000 + ordinal),
        coding_result=result,
    )


def _record(coordinator, component_id: str):
    return next(
        record
        for record in coordinator.snapshot().records
        if record.request.component_id == component_id
    )


def _run(coroutine):
    return asyncio.run(coroutine)


class FakeProgramWorker:
    def __init__(self) -> None:
        self.dispatch_calls = []
        self.inspect_calls = []
        self.recover_calls = []
        self.fail_dispatch: set[str] = set()
        self.invalid_base: set[str] = set()
        self.recovery_states: dict[str, RecoveryState | None] = {}
        self.on_dispatch: Callable[[object], None] | None = None
        self.delay_seconds = 0.0
        self.active = 0
        self.peak_active = 0

    async def dispatch(self, request):
        self.dispatch_calls.append(request)
        if self.on_dispatch is not None:
            self.on_dispatch(request)
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        try:
            if self.delay_seconds:
                await asyncio.sleep(self.delay_seconds)
            if request.component_id in self.fail_dispatch:
                raise RuntimeError("simulated external worker transport loss")
            envelope = _envelope(request, len(self.dispatch_calls))
            if request.component_id in self.invalid_base:
                return WorkerResultEnvelope(
                    work_id=envelope.work_id,
                    component_id=envelope.component_id,
                    repository_id=envelope.repository_id,
                    base_sha=SHA_B if request.base_sha != SHA_B else SHA_A,
                    result_sha=envelope.result_sha,
                    diff_digest=envelope.diff_digest,
                    coding_result=envelope.coding_result,
                )
            return envelope
        finally:
            self.active -= 1

    async def inspect(self, work_id):
        self.inspect_calls.append(work_id)
        return self.recovery_states.get(work_id)

    async def recover(self, request, state):
        self.recover_calls.append((request, state))
        return _envelope(request, 900 + len(self.recover_calls))


def test_dispatch_persists_running_and_pending_reservation_before_worker_call(tmp_path) -> None:
    store, _, binding, task_id, coordinator, _ = _setup(tmp_path)
    worker = FakeProgramWorker()
    checkpoints = ProductFactoryCheckpointHost(store)
    ledger = IdempotencyLedger(store)

    def assert_durable_before_dispatch(request) -> None:
        record = checkpoints.latest(host_task_id=task_id, project_id="project-1")
        assert record is not None
        durable = next(
            item
            for item in record.checkpoint.coordinator.records
            if item.request.component_id == request.component_id
        )
        assert durable.state is WorkState.RUNNING
        operation = ledger.require(f"pf-worker:{request.work_id}")
        assert operation.status is IdempotencyStatus.PENDING

    worker.on_dispatch = assert_durable_before_dispatch
    host = ProductFactoryProgramHost(store, worker)

    outcomes = _run(
        host.dispatch_ready(
            host_task_id=task_id,
            binding=binding,
            coordinator=coordinator,
            max_parallel=2,
        )
    )

    assert {item.disposition for item in outcomes} == {
        ProgramWorkDisposition.REVIEW_REQUIRED
    }
    assert all(item.operation_status is IdempotencyStatus.COMPLETED for item in outcomes)
    restored = host.restore_latest(host_task_id=task_id, binding=binding)
    assert _record(restored, "component-0").state is WorkState.REVIEW_REQUIRED
    assert _record(restored, "component-2").state is WorkState.REVIEW_REQUIRED
    assert _record(restored, "component-1").state is WorkState.PLANNED


def test_external_worker_failure_is_uncertain_and_does_not_cancel_independent_work(
    tmp_path,
) -> None:
    store, _, binding, task_id, coordinator, _ = _setup(tmp_path)
    worker = FakeProgramWorker()
    worker.fail_dispatch.add("component-0")
    host = ProductFactoryProgramHost(store, worker)

    outcomes = _run(
        host.dispatch_ready(
            host_task_id=task_id,
            binding=binding,
            coordinator=coordinator,
            max_parallel=2,
        )
    )

    by_component = {item.component_id: item for item in outcomes}
    assert by_component["component-0"].disposition is ProgramWorkDisposition.UNCERTAIN
    assert by_component["component-2"].disposition is ProgramWorkDisposition.REVIEW_REQUIRED
    ledger = IdempotencyLedger(store)
    failed_request = _record(coordinator, "component-0").request
    assert (
        ledger.require(f"pf-worker:{failed_request.work_id}").status
        is IdempotencyStatus.UNCERTAIN
    )
    restored = host.restore_latest(host_task_id=task_id, binding=binding)
    assert _record(restored, "component-0").state is WorkState.RUNNING
    assert _record(restored, "component-2").state is WorkState.REVIEW_REQUIRED


def test_running_checkpoint_without_ledger_is_proven_pre_dispatch_and_can_start_once(
    tmp_path,
) -> None:
    store, _, binding, task_id, coordinator, _ = _setup(tmp_path)
    request = coordinator.start("component-0")
    ProductFactoryCheckpointHost(store).save(
        host_task_id=task_id,
        checkpoint=binding.checkpoint(coordinator),
    )
    worker = FakeProgramWorker()
    restarted = ProductFactoryProgramHost(SQLiteStore(store.path), worker)
    restored = restarted.restore_latest(host_task_id=task_id, binding=binding)

    outcomes = _run(
        restarted.recover_running(
            host_task_id=task_id,
            binding=binding,
            coordinator=restored,
        )
    )

    assert len(worker.dispatch_calls) == 1
    assert worker.dispatch_calls[0].work_id == request.work_id
    assert outcomes[0].disposition is ProgramWorkDisposition.REVIEW_REQUIRED
    assert IdempotencyLedger(store).require(
        f"pf-worker:{request.work_id}"
    ).status is IdempotencyStatus.COMPLETED


def test_uncertain_worker_is_recovered_by_exact_work_id_without_redispatch(tmp_path) -> None:
    store, _, binding, task_id, coordinator, _ = _setup(tmp_path)
    first_worker = FakeProgramWorker()
    first_worker.fail_dispatch.add("component-0")
    first_host = ProductFactoryProgramHost(store, first_worker)
    _run(
        first_host.dispatch_ready(
            host_task_id=task_id,
            binding=binding,
            coordinator=coordinator,
            max_count=1,
        )
    )
    request = _record(coordinator, "component-0").request

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    project = ProductProjectRepository(restarted_store).get("project-1")
    restarted_binding = ProductProjectCoordinatorBinding(project, _graph())
    recovery_worker = FakeProgramWorker()
    recovery_worker.recovery_states[request.work_id] = RecoveryState(
        "interrupted",
        "opaque-resume-token",
    )
    restarted_host = ProductFactoryProgramHost(restarted_store, recovery_worker)
    restored = restarted_host.restore_latest(
        host_task_id=task_id,
        binding=restarted_binding,
    )

    outcomes = _run(
        restarted_host.recover_running(
            host_task_id=task_id,
            binding=restarted_binding,
            coordinator=restored,
        )
    )

    assert recovery_worker.dispatch_calls == []
    assert recovery_worker.inspect_calls == [request.work_id]
    assert recovery_worker.recover_calls[0][0].work_id == request.work_id
    assert outcomes[0].disposition is ProgramWorkDisposition.REVIEW_REQUIRED
    assert IdempotencyLedger(restarted_store).require(
        f"pf-worker:{request.work_id}"
    ).status is IdempotencyStatus.COMPLETED


def test_missing_worker_recovery_state_blocks_only_that_component_and_forbids_replay(
    tmp_path,
) -> None:
    store, _, binding, task_id, coordinator, _ = _setup(tmp_path)
    first_worker = FakeProgramWorker()
    first_worker.fail_dispatch.add("component-0")
    first_host = ProductFactoryProgramHost(store, first_worker)
    _run(
        first_host.dispatch_ready(
            host_task_id=task_id,
            binding=binding,
            coordinator=coordinator,
            max_count=1,
        )
    )
    request = _record(coordinator, "component-0").request

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    project = ProductProjectRepository(restarted_store).get("project-1")
    restarted_binding = ProductProjectCoordinatorBinding(project, _graph())
    recovery_worker = FakeProgramWorker()
    recovery_worker.recovery_states[request.work_id] = None
    restarted_host = ProductFactoryProgramHost(restarted_store, recovery_worker)
    restored = restarted_host.restore_latest(
        host_task_id=task_id,
        binding=restarted_binding,
    )

    outcomes = _run(
        restarted_host.recover_running(
            host_task_id=task_id,
            binding=restarted_binding,
            coordinator=restored,
        )
    )

    assert outcomes[0].disposition is ProgramWorkDisposition.BLOCKED_MISSING_WORKER_STATE
    assert recovery_worker.dispatch_calls == []
    assert recovery_worker.recover_calls == []
    assert _record(restored, "component-0").state is WorkState.BLOCKED
    assert "component-2" in {item.component_id for item in restored.ready_requests()}
    assert IdempotencyLedger(restarted_store).require(
        f"pf-worker:{request.work_id}"
    ).status is IdempotencyStatus.UNCERTAIN


def test_invalid_worker_evidence_remains_running_and_marks_operation_uncertain(tmp_path) -> None:
    store, _, binding, task_id, coordinator, _ = _setup(tmp_path)
    worker = FakeProgramWorker()
    worker.invalid_base.add("component-0")
    host = ProductFactoryProgramHost(store, worker)

    outcomes = _run(
        host.dispatch_ready(
            host_task_id=task_id,
            binding=binding,
            coordinator=coordinator,
            max_count=1,
        )
    )

    assert outcomes[0].disposition is ProgramWorkDisposition.UNCERTAIN
    assert _record(coordinator, "component-0").state is WorkState.RUNNING
    restored = host.restore_latest(host_task_id=task_id, binding=binding)
    assert _record(restored, "component-0").state is WorkState.RUNNING


def test_durable_result_with_pending_ledger_reconciles_after_restart_without_worker_call(
    tmp_path,
) -> None:
    store, _, binding, task_id, coordinator, _ = _setup(tmp_path)
    worker = FakeProgramWorker()

    class FailingCompleteLedger(IdempotencyLedger):
        def complete(self, operation_key, result=None):
            raise OSError("simulated ledger write outage")

    host = ProductFactoryProgramHost(
        store,
        worker,
        idempotency=FailingCompleteLedger(store),
    )
    outcomes = _run(
        host.dispatch_ready(
            host_task_id=task_id,
            binding=binding,
            coordinator=coordinator,
            max_count=1,
        )
    )

    assert outcomes[0].disposition is ProgramWorkDisposition.NEEDS_RECONCILIATION
    request = _record(coordinator, "component-0").request
    assert IdempotencyLedger(store).require(
        f"pf-worker:{request.work_id}"
    ).status is IdempotencyStatus.PENDING

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    project = ProductProjectRepository(restarted_store).get("project-1")
    restarted_binding = ProductProjectCoordinatorBinding(project, _graph())
    no_worker = FakeProgramWorker()
    restarted_host = ProductFactoryProgramHost(restarted_store, no_worker)
    restored = restarted_host.restore_latest(
        host_task_id=task_id,
        binding=restarted_binding,
    )

    assert no_worker.dispatch_calls == []
    assert _record(restored, "component-0").state is WorkState.REVIEW_REQUIRED
    assert IdempotencyLedger(restarted_store).require(
        f"pf-worker:{request.work_id}"
    ).status is IdempotencyStatus.COMPLETED


def test_typed_worker_failure_is_durable_repair_not_uncertain_transport(tmp_path) -> None:
    store, _, binding, task_id, coordinator, _ = _setup(tmp_path)

    class TypedFailureWorker(FakeProgramWorker):
        async def dispatch(self, request):
            self.dispatch_calls.append(request)
            return _envelope(
                request,
                1,
                failure=WorkerFailure(
                    WorkerFailureKind.PROCESS_FAILED,
                    "deterministic tests failed",
                    retryable=True,
                ),
            )

    worker = TypedFailureWorker()
    host = ProductFactoryProgramHost(store, worker)

    outcomes = _run(
        host.dispatch_ready(
            host_task_id=task_id,
            binding=binding,
            coordinator=coordinator,
            max_count=1,
        )
    )

    assert outcomes[0].disposition is ProgramWorkDisposition.REPAIR_REQUIRED
    assert outcomes[0].operation_status is IdempotencyStatus.COMPLETED
    assert _record(coordinator, "component-0").state is WorkState.REPAIR_REQUIRED


def test_review_reject_and_repair_checkpoint_survive_two_restarts_with_new_identity(
    tmp_path,
) -> None:
    store, _, binding, task_id, coordinator, graph = _setup(tmp_path)
    host = ProductFactoryProgramHost(store, FakeProgramWorker())
    _run(
        host.dispatch_ready(
            host_task_id=task_id,
            binding=binding,
            coordinator=coordinator,
            max_count=1,
        )
    )
    original = _record(coordinator, "component-0").request
    host.review_and_checkpoint(
        host_task_id=task_id,
        binding=binding,
        coordinator=coordinator,
        component_id="component-0",
        decision=ReviewDecision(
            reviewer_id="qa-independent",
            accepted=False,
            reason="security findings require repair",
            evidence_refs=("review:reject",),
        ),
    )

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    project = ProductProjectRepository(restarted_store).get("project-1")
    binding = ProductProjectCoordinatorBinding(project, graph)
    host = ProductFactoryProgramHost(restarted_store, FakeProgramWorker())
    coordinator = host.restore_latest(host_task_id=task_id, binding=binding)
    assert _record(coordinator, "component-0").state is WorkState.REPAIR_REQUIRED

    repaired = host.prepare_repair_and_checkpoint(
        host_task_id=task_id,
        binding=binding,
        coordinator=coordinator,
        component_id="component-0",
        base_sha="f" * 40,
        reason="apply independent security review",
    )
    assert repaired.attempt == 2
    assert repaired.work_id != original.work_id

    second_store = SQLiteStore(restarted_store.path)
    second_store.initialize()
    project = ProductProjectRepository(second_store).get("project-1")
    binding = ProductProjectCoordinatorBinding(project, graph)
    coordinator = ProductFactoryProgramHost(
        second_store,
        FakeProgramWorker(),
    ).restore_latest(host_task_id=task_id, binding=binding)
    record = _record(coordinator, "component-0")
    assert record.state is WorkState.READY
    assert record.request.work_id == repaired.work_id
    assert record.request.base_sha == "f" * 40


def test_review_accept_checkpoint_unlocks_dependency_after_restart(tmp_path) -> None:
    store, _, binding, task_id, coordinator, graph = _setup(tmp_path)
    host = ProductFactoryProgramHost(store, FakeProgramWorker())
    _run(
        host.dispatch_ready(
            host_task_id=task_id,
            binding=binding,
            coordinator=coordinator,
            max_count=1,
        )
    )
    host.review_and_checkpoint(
        host_task_id=task_id,
        binding=binding,
        coordinator=coordinator,
        component_id="component-0",
        decision=ReviewDecision(
            reviewer_id="qa-independent",
            accepted=True,
            reason="exact evidence independently accepted",
            evidence_refs=("review:accept",),
        ),
    )

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    project = ProductProjectRepository(restarted_store).get("project-1")
    binding = ProductProjectCoordinatorBinding(project, graph)
    restored = ProductFactoryProgramHost(
        restarted_store,
        FakeProgramWorker(),
    ).restore_latest(host_task_id=task_id, binding=binding)

    assert _record(restored, "component-0").state is WorkState.ACCEPTED
    assert "component-1" in {item.component_id for item in restored.ready_requests()}


def test_bounded_parallel_dispatch_reaches_limit_without_exceeding_it(tmp_path) -> None:
    store, _, binding, task_id, coordinator, _ = _setup(
        tmp_path,
        component_count=5,
        repository_count=5,
    )
    worker = FakeProgramWorker()
    worker.delay_seconds = 0.02
    host = ProductFactoryProgramHost(store, worker)

    outcomes = _run(
        host.dispatch_ready(
            host_task_id=task_id,
            binding=binding,
            coordinator=coordinator,
            max_parallel=2,
            max_count=5,
        )
    )

    assert len(outcomes) == 5
    assert worker.peak_active == 2
    assert all(item.disposition is ProgramWorkDisposition.REVIEW_REQUIRED for item in outcomes)


def test_max_count_bounds_one_dispatch_batch_and_preserves_remaining_ready_work(tmp_path) -> None:
    store, _, binding, task_id, coordinator, _ = _setup(
        tmp_path,
        component_count=8,
        repository_count=8,
    )
    worker = FakeProgramWorker()
    host = ProductFactoryProgramHost(store, worker)

    outcomes = _run(
        host.dispatch_ready(
            host_task_id=task_id,
            binding=binding,
            coordinator=coordinator,
            max_parallel=3,
            max_count=3,
        )
    )

    assert len(outcomes) == 3
    assert len(worker.dispatch_calls) == 3
    assert len(coordinator.ready_requests()) == 5


def test_stale_product_project_refuses_program_resume_before_worker_access(tmp_path) -> None:
    store, projects, binding, task_id, coordinator, graph = _setup(tmp_path)
    host = ProductFactoryProgramHost(store, FakeProgramWorker())
    _run(
        host.dispatch_ready(
            host_task_id=task_id,
            binding=binding,
            coordinator=coordinator,
            max_count=1,
        )
    )
    updated = projects.update_spec(
        "project-1",
        _spec(graph, "Build changed product specification"),
        expected_row_version=binding.project.row_version,
    )
    stale_binding = ProductProjectCoordinatorBinding(updated, graph)
    worker = FakeProgramWorker()
    restarted = ProductFactoryProgramHost(SQLiteStore(store.path), worker)

    with pytest.raises(ProductFactoryProgramError, match="not resumable"):
        restarted.restore_latest(host_task_id=task_id, binding=stale_binding)

    assert worker.dispatch_calls == []
    assert worker.inspect_calls == []
    assert worker.recover_calls == []


def test_completed_ledger_with_running_checkpoint_never_redispatches(tmp_path) -> None:
    store, _, binding, task_id, coordinator, _ = _setup(tmp_path)
    worker = FakeProgramWorker()
    worker.fail_dispatch.add("component-0")
    host = ProductFactoryProgramHost(store, worker)
    _run(
        host.dispatch_ready(
            host_task_id=task_id,
            binding=binding,
            coordinator=coordinator,
            max_count=1,
        )
    )
    request = _record(coordinator, "component-0").request
    ledger = IdempotencyLedger(store)
    ledger.reconcile_completed(
        f"pf-worker:{request.work_id}",
        {"manual_reconciliation": "external system proved completion"},
    )

    recovery_worker = FakeProgramWorker()
    restarted = ProductFactoryProgramHost(SQLiteStore(store.path), recovery_worker)
    restored = restarted.restore_latest(host_task_id=task_id, binding=binding)
    outcomes = _run(
        restarted.recover_running(
            host_task_id=task_id,
            binding=binding,
            coordinator=restored,
        )
    )

    assert outcomes[0].disposition is ProgramWorkDisposition.NEEDS_RECONCILIATION
    assert recovery_worker.dispatch_calls == []
    assert recovery_worker.inspect_calls == []
    assert recovery_worker.recover_calls == []


def test_twenty_five_components_progress_through_five_restart_waves_without_duplicate_dispatch(
    tmp_path,
) -> None:
    store, _, binding, task_id, coordinator, graph = _setup(
        tmp_path,
        component_count=25,
        repository_count=5,
    )
    all_work_ids: list[str] = []
    wave_count = 0

    while coordinator.ready_requests():
        worker = FakeProgramWorker()
        host = ProductFactoryProgramHost(store, worker)
        outcomes = _run(
            host.dispatch_ready(
                host_task_id=task_id,
                binding=binding,
                coordinator=coordinator,
                max_parallel=5,
                max_count=5,
            )
        )
        assert all(
            item.disposition is ProgramWorkDisposition.REVIEW_REQUIRED for item in outcomes
        )
        for outcome in outcomes:
            all_work_ids.append(outcome.work_id)
            host.review_and_checkpoint(
                host_task_id=task_id,
                binding=binding,
                coordinator=coordinator,
                component_id=outcome.component_id,
                decision=ReviewDecision(
                    reviewer_id=f"qa-{outcome.component_id}",
                    accepted=True,
                    reason="independent wave review accepted exact evidence",
                    evidence_refs=(f"review:{outcome.component_id}",),
                ),
            )

        restarted_store = SQLiteStore(store.path)
        restarted_store.initialize()
        project = ProductProjectRepository(restarted_store).get("project-1")
        binding = ProductProjectCoordinatorBinding(project, graph)
        coordinator = ProductFactoryProgramHost(
            restarted_store,
            FakeProgramWorker(),
        ).restore_latest(host_task_id=task_id, binding=binding)
        store = restarted_store
        wave_count += 1

    assert wave_count == 5
    assert len(all_work_ids) == 25
    assert len(set(all_work_ids)) == 25
    assert all(record.state is WorkState.ACCEPTED for record in coordinator.snapshot().records)


def test_invalid_program_bounds_fail_before_any_coordinator_mutation(tmp_path) -> None:
    store, _, binding, task_id, coordinator, _ = _setup(tmp_path)
    host = ProductFactoryProgramHost(store, FakeProgramWorker())
    before = coordinator.snapshot()

    with pytest.raises(ValueError, match="positive"):
        _run(
            host.dispatch_ready(
                host_task_id=task_id,
                binding=binding,
                coordinator=coordinator,
                max_parallel=0,
            )
        )

    assert coordinator.snapshot() == before


def test_wrong_worker_result_identity_is_uncertain_and_never_unlocks_dependency(tmp_path) -> None:
    store, _, binding, task_id, coordinator, _ = _setup(tmp_path)

    class WrongIdentityWorker(FakeProgramWorker):
        async def dispatch(self, request):
            self.dispatch_calls.append(request)
            envelope = _envelope(request)
            return WorkerResultEnvelope(
                work_id="foreign-work",
                component_id=envelope.component_id,
                repository_id=envelope.repository_id,
                base_sha=envelope.base_sha,
                result_sha=envelope.result_sha,
                diff_digest=envelope.diff_digest,
                coding_result=envelope.coding_result,
            )

    host = ProductFactoryProgramHost(store, WrongIdentityWorker())
    outcomes = _run(
        host.dispatch_ready(
            host_task_id=task_id,
            binding=binding,
            coordinator=coordinator,
            max_count=1,
        )
    )

    assert outcomes[0].disposition is ProgramWorkDisposition.UNCERTAIN
    assert _record(coordinator, "component-0").state is WorkState.RUNNING
    assert _record(coordinator, "component-1").state is WorkState.PLANNED


def test_program_host_dispatches_through_existing_public_coding_worker_adapter(tmp_path) -> None:
    store, _, binding, task_id, coordinator, _ = _setup(tmp_path)

    class AdapterContexts:
        async def context_for(self, request):
            return CodingWorkerDispatchContext(
                repository_tree_digest="tree-v1",
                lease=WorkspaceLease(
                    lease_id=f"lease:{request.work_id}",
                    workspace_root=Path("worker-root") / request.component_id,
                    isolation_class=IsolationClass.PROCESS_CONTAINED,
                    expires_at="2026-08-21T00:00:00Z",
                ),
                process_policy=ProcessPolicy(("python",)),
                network_policy=NetworkPolicy(),
                resource_budget=ResourceBudget(300, 1024 * 1024, 20),
            )

    class AdapterEvidence:
        async def collect(self, request, job, result):
            assert job.job_id == request.work_id
            assert job.task_id == f"product:{request.project_id}:component:{request.component_id}"
            assert job.allowed_paths.roots == request.allowed_paths
            assert job.permission_ceiling == request.permission_ceiling
            assert result.job_id == request.work_id
            return CodingWorkerExecutionEvidence(
                work_id=request.work_id,
                repository_id=request.repository_id,
                base_sha=request.base_sha,
                result_sha=SHA_B,
                diff_digest=DIGEST,
            )

    class PublicCodingWorker:
        def __init__(self) -> None:
            self.jobs = []

        async def execute(self, job):
            self.jobs.append(job)
            return CodingResult(
                job_id=job.job_id,
                test_evidence=(
                    WorkerTestEvidence(
                        ("python", "-m", "pytest", "tests/component-0"),
                        0,
                        "worker-tests-ok",
                    ),
                ),
            )

        async def cancel(self, job_id):
            raise AssertionError(f"unexpected cancel for {job_id}")

        async def inspect(self, job_id):
            raise AssertionError(f"unexpected inspect for {job_id}")

        async def recover(self, job, state):
            raise AssertionError(f"unexpected recover for {job.job_id}: {state.phase}")

    public_worker = PublicCodingWorker()
    adapter = CodingWorkerComponentAdapter(
        public_worker,
        AdapterContexts(),
        AdapterEvidence(),
    )
    host = ProductFactoryProgramHost(store, adapter)

    outcomes = _run(
        host.dispatch_ready(
            host_task_id=task_id,
            binding=binding,
            coordinator=coordinator,
            max_count=1,
        )
    )

    assert outcomes[0].disposition is ProgramWorkDisposition.REVIEW_REQUIRED
    assert len(public_worker.jobs) == 1
    job = public_worker.jobs[0]
    assert job.repository.repository_id == "repo-0"
    assert job.repository.base_sha == _sha(1)
    assert job.acceptance_commands[0].argv == (
        "python",
        "-m",
        "pytest",
        "tests/component-0",
    )
