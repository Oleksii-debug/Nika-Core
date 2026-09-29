from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import nika_core.product_factory_program_host as program_host_module
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
from nika_core.product_factory_work_ownership import (
    ProductFactoryWorkOwnership,
    WorkOwnershipError,
)
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
                    request.acceptance_commands[0],
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


def test_uncontained_batch_failure_cancels_and_settles_admitted_sibling_before_return(
    tmp_path,
) -> None:
    store, _, binding, task_id, coordinator, _ = _setup(
        tmp_path,
        component_count=2,
        repository_count=2,
    )
    sibling_started = asyncio.Event()
    sibling_cancelled = asyncio.Event()

    class BlockingWorker(FakeProgramWorker):
        async def dispatch(self, request):
            self.dispatch_calls.append(request)
            sibling_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                sibling_cancelled.set()
                raise

    class OneChildFailsHost(ProductFactoryProgramHost):
        async def _dispatch_one(self, **kwargs):
            request = kwargs["request"]
            if request.component_id == "component-0":
                await sibling_started.wait()
                self._release_best_effort(kwargs["lease"])
                raise ProductFactoryProgramError("forced uncontained batch child failure")
            return await super()._dispatch_one(**kwargs)

    worker = BlockingWorker()
    host = OneChildFailsHost(
        store,
        worker,
        owner_id="program-host:batch-settlement",
    )

    async def scenario() -> None:
        with pytest.raises(ProductFactoryProgramError, match="uncontained batch child failure"):
            await host.dispatch_ready(
                host_task_id=task_id,
                binding=binding,
                coordinator=coordinator,
                max_parallel=2,
                max_count=2,
            )
        assert sibling_cancelled.is_set()

    _run(scenario())

    sibling = _record(coordinator, "component-1").request
    failed = _record(coordinator, "component-0").request
    ledger = IdempotencyLedger(store)
    assert [item.component_id for item in worker.dispatch_calls] == ["component-1"]
    assert ledger.get(f"pf-worker:{failed.work_id}") is None
    assert (
        ledger.require(f"pf-worker:{sibling.work_id}").status
        is IdempotencyStatus.UNCERTAIN
    )
    assert host._ownership.current(
        project_id=failed.project_id,
        work_id=failed.work_id,
    ) is None
    assert host._ownership.current(
        project_id=sibling.project_id,
        work_id=sibling.work_id,
    ) is None


def test_uncontained_recovery_batch_failure_settles_sibling_before_return(tmp_path) -> None:
    store, _, binding, task_id, coordinator, _ = _setup(
        tmp_path,
        component_count=2,
        repository_count=2,
    )
    coordinator.start("component-0")
    coordinator.start("component-1")
    sibling_started = asyncio.Event()
    sibling_cancelled = asyncio.Event()

    class BlockingWorker(FakeProgramWorker):
        async def dispatch(self, request):
            self.dispatch_calls.append(request)
            sibling_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                sibling_cancelled.set()
                raise

    class OneRecoveryChildFailsHost(ProductFactoryProgramHost):
        async def _recover_one(self, **kwargs):
            record = kwargs["record"]
            if record.request.component_id == "component-0":
                await sibling_started.wait()
                raise ProductFactoryProgramError("forced uncontained recovery child failure")
            return await super()._recover_one(**kwargs)

    worker = BlockingWorker()
    host = OneRecoveryChildFailsHost(
        store,
        worker,
        owner_id="program-host:recovery-batch-settlement",
    )

    async def scenario() -> None:
        with pytest.raises(ProductFactoryProgramError, match="uncontained recovery child failure"):
            await host.recover_running(
                host_task_id=task_id,
                binding=binding,
                coordinator=coordinator,
                max_parallel=2,
            )
        assert sibling_cancelled.is_set()

    _run(scenario())

    sibling = _record(coordinator, "component-1").request
    ledger = IdempotencyLedger(store)
    assert [item.component_id for item in worker.dispatch_calls] == ["component-1"]
    assert (
        ledger.require(f"pf-worker:{sibling.work_id}").status
        is IdempotencyStatus.UNCERTAIN
    )
    assert host._ownership.current(
        project_id=sibling.project_id,
        work_id=sibling.work_id,
    ) is None


def test_cancelled_dispatch_child_settles_blocking_sibling_before_propagation(
    tmp_path,
) -> None:
    store, _, binding, task_id, coordinator, _ = _setup(
        tmp_path,
        component_count=2,
        repository_count=2,
    )
    sibling_started = asyncio.Event()
    sibling_cancelled = asyncio.Event()

    class BlockingWorker(FakeProgramWorker):
        async def dispatch(self, request):
            self.dispatch_calls.append(request)
            sibling_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                sibling_cancelled.set()
                raise

    class OneChildCancelsHost(ProductFactoryProgramHost):
        async def _dispatch_one(self, **kwargs):
            request = kwargs["request"]
            if request.component_id == "component-0":
                await sibling_started.wait()
                self._release_best_effort(kwargs["lease"])
                raise asyncio.CancelledError()
            return await super()._dispatch_one(**kwargs)

    worker = BlockingWorker()
    host = OneChildCancelsHost(
        store,
        worker,
        owner_id="program-host:cancelled-batch-child",
    )

    async def scenario() -> None:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(
                host.dispatch_ready(
                    host_task_id=task_id,
                    binding=binding,
                    coordinator=coordinator,
                    max_parallel=2,
                    max_count=2,
                ),
                timeout=0.5,
            )
        assert sibling_cancelled.is_set()

    _run(scenario())

    sibling = _record(coordinator, "component-1").request
    failed = _record(coordinator, "component-0").request
    ledger = IdempotencyLedger(store)
    assert [item.component_id for item in worker.dispatch_calls] == ["component-1"]
    assert ledger.get(f"pf-worker:{failed.work_id}") is None
    assert (
        ledger.require(f"pf-worker:{sibling.work_id}").status
        is IdempotencyStatus.UNCERTAIN
    )
    assert host._ownership.current(
        project_id=failed.project_id,
        work_id=failed.work_id,
    ) is None
    assert host._ownership.current(
        project_id=sibling.project_id,
        work_id=sibling.work_id,
    ) is None


def test_cancelled_recovery_child_settles_blocking_sibling_before_propagation(
    tmp_path,
) -> None:
    store, _, binding, task_id, coordinator, _ = _setup(
        tmp_path,
        component_count=2,
        repository_count=2,
    )
    coordinator.start("component-0")
    coordinator.start("component-1")
    sibling_started = asyncio.Event()
    sibling_cancelled = asyncio.Event()

    class BlockingWorker(FakeProgramWorker):
        async def dispatch(self, request):
            self.dispatch_calls.append(request)
            sibling_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                sibling_cancelled.set()
                raise

    class OneRecoveryChildCancelsHost(ProductFactoryProgramHost):
        async def _recover_one(self, **kwargs):
            record = kwargs["record"]
            if record.request.component_id == "component-0":
                await sibling_started.wait()
                raise asyncio.CancelledError()
            return await super()._recover_one(**kwargs)

    worker = BlockingWorker()
    host = OneRecoveryChildCancelsHost(
        store,
        worker,
        owner_id="program-host:cancelled-recovery-child",
    )

    async def scenario() -> None:
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(
                host.recover_running(
                    host_task_id=task_id,
                    binding=binding,
                    coordinator=coordinator,
                    max_parallel=2,
                ),
                timeout=0.5,
            )
        assert sibling_cancelled.is_set()

    _run(scenario())

    sibling = _record(coordinator, "component-1").request
    ledger = IdempotencyLedger(store)
    assert [item.component_id for item in worker.dispatch_calls] == ["component-1"]
    assert (
        ledger.require(f"pf-worker:{sibling.work_id}").status
        is IdempotencyStatus.UNCERTAIN
    )
    assert host._ownership.current(
        project_id=sibling.project_id,
        work_id=sibling.work_id,
    ) is None


def test_external_effect_cleanup_is_bounded_when_worker_ignores_first_cancel(
    monkeypatch,
) -> None:
    first_cancel_seen = asyncio.Event()
    release = asyncio.Event()

    async def cancellation_resistant_effect() -> None:
        try:
            await release.wait()
        except asyncio.CancelledError:
            first_cancel_seen.set()
            await release.wait()

    async def scenario() -> None:
        monkeypatch.setattr(program_host_module, "_EFFECT_CANCEL_GRACE_SECONDS", 0.01)
        task = asyncio.create_task(cancellation_resistant_effect())
        await asyncio.sleep(0)

        await asyncio.wait_for(
            program_host_module._cancel_effect_task(task),
            timeout=0.2,
        )

        assert first_cancel_seen.is_set()
        assert not task.done()

        release.set()
        await asyncio.wait_for(task, timeout=0.2)

    _run(scenario())


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
    seed_host = ProductFactoryProgramHost(store, FakeProgramWorker())
    request = coordinator.start("component-0")
    lease = seed_host._acquire(request)
    try:
        seed_host._checkpoint_running(
            host_task_id=task_id,
            binding=binding,
            coordinator=coordinator,
            requests=(request,),
            leases=(lease,),
        )
        operation, created = seed_host._reserve_effect(
            host_task_id=task_id,
            request=request,
            lease=lease,
        )
        assert created is True
        assert operation.status is IdempotencyStatus.PENDING

        updated = coordinator.record_result(_envelope(request))
        assert updated.state is WorkState.REVIEW_REQUIRED
        seed_host._save_fenced(task_id, binding, coordinator, lease)
    finally:
        seed_host._release_best_effort(lease)

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


class _BehavioralProgramBound(int):
    def __le__(self, other):  # pragma: no cover - must never execute
        raise AssertionError("program bound comparison executed before exact-type validation")


@pytest.mark.parametrize(
    ("operation", "kwargs"),
    (
        ("dispatch", {"max_parallel": True}),
        ("dispatch", {"max_count": True}),
        ("dispatch", {"max_parallel": _BehavioralProgramBound(1)}),
        ("dispatch", {"max_count": _BehavioralProgramBound(1)}),
        ("recover", {"max_parallel": True}),
        ("recover", {"max_parallel": _BehavioralProgramBound(1)}),
    ),
)
def test_program_bounds_require_exact_integers_before_behavior(
    tmp_path,
    operation: str,
    kwargs: dict[str, object],
) -> None:
    store, _, binding, task_id, coordinator, _ = _setup(tmp_path)
    host = ProductFactoryProgramHost(store, FakeProgramWorker())
    before = coordinator.snapshot()

    if operation == "dispatch":
        awaitable = host.dispatch_ready(
            host_task_id=task_id,
            binding=binding,
            coordinator=coordinator,
            **kwargs,
        )
    else:
        awaitable = host.recover_running(
            host_task_id=task_id,
            binding=binding,
            coordinator=coordinator,
            **kwargs,
        )

    with pytest.raises(ValueError, match="exact positive integer"):
        _run(awaitable)
    assert coordinator.snapshot() == before


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


class _MutableOwnershipClock:
    def __init__(self, instant: datetime) -> None:
        self.instant = instant

    def __call__(self) -> datetime:
        return self.instant

    def advance(self, **delta: int) -> None:
        self.instant += timedelta(**delta)


def _seed_running_pending(host, coordinator, binding, task_id):
    request = coordinator.start("component-0")
    lease = host._acquire(request)
    try:
        host._checkpoint_running(
            host_task_id=task_id,
            binding=binding,
            coordinator=coordinator,
            requests=(request,),
            leases=(lease,),
        )
        operation, created = host._reserve_effect(
            host_task_id=task_id,
            request=request,
            lease=lease,
        )
        assert created is True
        assert operation.status is IdempotencyStatus.PENDING
    finally:
        host._release_best_effort(lease)
    return request, lease


def test_recovery_wait_cancellation_releases_exact_lease_without_changing_pending(
    tmp_path,
) -> None:
    store, _, binding, task_id, coordinator, _ = _setup(tmp_path)
    worker = FakeProgramWorker()
    host = ProductFactoryProgramHost(store, worker, owner_id="program-host:first")
    request, seed_lease = _seed_running_pending(host, coordinator, binding, task_id)

    async def scenario() -> None:
        task = asyncio.create_task(
            host._recover_one(
                semaphore=asyncio.Semaphore(0),
                host_task_id=task_id,
                binding=binding,
                coordinator=coordinator,
                record=_record(coordinator, "component-0"),
            )
        )
        for _ in range(100):
            if host._ownership.current(
                project_id=request.project_id,
                work_id=request.work_id,
            ) is not None:
                break
            await asyncio.sleep(0)
        else:
            raise AssertionError("recovery lease was not acquired")

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    _run(scenario())

    assert host._ownership.current(
        project_id=request.project_id,
        work_id=request.work_id,
    ) is None
    assert IdempotencyLedger(store).require(
        f"pf-worker:{request.work_id}"
    ).status is IdempotencyStatus.PENDING
    assert worker.dispatch_calls == []
    assert worker.inspect_calls == []
    assert worker.recover_calls == []

    replacement = ProductFactoryWorkOwnership(store).acquire(
        project_id=request.project_id,
        work_id=request.work_id,
        owner_id="program-host:replacement",
        lease_seconds=300,
    )
    assert replacement.fence > seed_lease.fence


def test_recovery_ledger_read_failure_releases_lease_before_any_worker_effect(tmp_path) -> None:
    store, _, binding, task_id, coordinator, _ = _setup(tmp_path)
    request = coordinator.start("component-0")

    class FailingReadLedger(IdempotencyLedger):
        def get(self, operation_key):
            raise RuntimeError(f"cannot read {operation_key}")

    worker = FakeProgramWorker()
    host = ProductFactoryProgramHost(
        store,
        worker,
        idempotency=FailingReadLedger(store),
        owner_id="program-host:read-failure",
    )

    with pytest.raises(RuntimeError, match="cannot read"):
        _run(
            host._recover_one(
                semaphore=asyncio.Semaphore(1),
                host_task_id=task_id,
                binding=binding,
                coordinator=coordinator,
                record=_record(coordinator, "component-0"),
            )
        )

    assert host._ownership.current(
        project_id=request.project_id,
        work_id=request.work_id,
    ) is None
    assert worker.dispatch_calls == []
    assert worker.inspect_calls == []
    assert worker.recover_calls == []


def test_recovery_pre_effect_renew_failure_releases_lease_without_worker_call(tmp_path) -> None:
    store, _, binding, task_id, coordinator, _ = _setup(tmp_path)
    request = coordinator.start("component-0")

    class FailingRenewOwnership(ProductFactoryWorkOwnership):
        def renew(self, **kwargs):
            raise WorkOwnershipError("forced stale authority")

    authority = FailingRenewOwnership(store)
    worker = FakeProgramWorker()
    host = ProductFactoryProgramHost(
        store,
        worker,
        ownership=authority,
        owner_id="program-host:renew-failure",
    )

    with pytest.raises(ProductFactoryProgramError, match="stale Product Factory authority"):
        _run(
            host._recover_one(
                semaphore=asyncio.Semaphore(1),
                host_task_id=task_id,
                binding=binding,
                coordinator=coordinator,
                record=_record(coordinator, "component-0"),
            )
        )

    assert authority.current(
        project_id=request.project_id,
        work_id=request.work_id,
    ) is None
    assert worker.dispatch_calls == []
    assert worker.inspect_calls == []
    assert worker.recover_calls == []


def test_long_dispatch_renews_same_fence_before_original_expiry_allows_takeover(
    monkeypatch,
    tmp_path,
) -> None:
    store, _, binding, task_id, coordinator, _ = _setup(tmp_path)
    clock = _MutableOwnershipClock(datetime(2026, 9, 27, 12, 0, tzinfo=UTC))
    authority = ProductFactoryWorkOwnership(store, clock=clock)
    started = asyncio.Event()
    finish = asyncio.Event()

    class BlockingWorker(FakeProgramWorker):
        async def dispatch(self, request):
            self.dispatch_calls.append(request)
            started.set()
            await finish.wait()
            return _envelope(request)

    worker = BlockingWorker()
    host = ProductFactoryProgramHost(
        store,
        worker,
        ownership=authority,
        owner_id="program-host:heartbeat",
        lease_seconds=3,
    )
    monkeypatch.setattr(program_host_module, "_lease_heartbeat_interval", lambda _: 0.001)

    async def scenario():
        task = asyncio.create_task(
            host.dispatch_ready(
                host_task_id=task_id,
                binding=binding,
                coordinator=coordinator,
                max_count=1,
            )
        )
        await started.wait()
        request = _record(coordinator, "component-0").request
        original = authority.current(
            project_id=request.project_id,
            work_id=request.work_id,
        )
        assert original is not None

        clock.advance(seconds=2)
        for _ in range(200):
            refreshed = authority.current(
                project_id=request.project_id,
                work_id=request.work_id,
            )
            if refreshed is not None and refreshed.expires_at > original.expires_at:
                break
            await asyncio.sleep(0.001)
        else:
            raise AssertionError("lease heartbeat did not extend exact fence")

        assert refreshed.fence == original.fence
        clock.advance(seconds=2)
        with pytest.raises(WorkOwnershipError, match="active owner"):
            ProductFactoryWorkOwnership(store, clock=clock).acquire(
                project_id=request.project_id,
                work_id=request.work_id,
                owner_id="program-host:competitor",
                lease_seconds=3,
            )

        finish.set()
        return await task

    outcomes = _run(scenario())
    assert outcomes[0].disposition is ProgramWorkDisposition.REVIEW_REQUIRED
    assert len(worker.dispatch_calls) == 1


def test_dispatch_queue_renews_exact_fence_while_waiting_for_semaphore(
    monkeypatch,
    tmp_path,
) -> None:
    store, _, binding, task_id, coordinator, _ = _setup(
        tmp_path,
        component_count=2,
        repository_count=2,
    )
    clock = _MutableOwnershipClock(datetime(2026, 9, 28, 10, 0, tzinfo=UTC))
    authority = ProductFactoryWorkOwnership(store, clock=clock)
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    class QueueBlockingWorker(FakeProgramWorker):
        async def dispatch(self, request):
            self.dispatch_calls.append(request)
            if len(self.dispatch_calls) == 1:
                first_started.set()
                await release_first.wait()
            return _envelope(request, len(self.dispatch_calls))

    worker = QueueBlockingWorker()
    host = ProductFactoryProgramHost(
        store,
        worker,
        ownership=authority,
        owner_id="program-host:queued-dispatch",
        lease_seconds=3,
    )
    monkeypatch.setattr(program_host_module, "_lease_heartbeat_interval", lambda _: 0.01)

    async def scenario():
        task = asyncio.create_task(
            host.dispatch_ready(
                host_task_id=task_id,
                binding=binding,
                coordinator=coordinator,
                max_parallel=1,
                max_count=2,
            )
        )
        await first_started.wait()
        first_component = worker.dispatch_calls[0].component_id
        queued = next(
            _record(coordinator, component_id).request
            for component_id in ("component-0", "component-1")
            if component_id != first_component
        )
        original = authority.current(
            project_id=queued.project_id,
            work_id=queued.work_id,
        )
        assert original is not None

        clock.advance(seconds=2)
        for _ in range(200):
            refreshed = authority.current(
                project_id=queued.project_id,
                work_id=queued.work_id,
            )
            if refreshed is not None and refreshed.expires_at > original.expires_at:
                break
            await asyncio.sleep(0.002)
        else:
            raise AssertionError("queued dispatch lease heartbeat did not extend exact fence")

        assert refreshed.fence == original.fence
        clock.advance(seconds=2)
        with pytest.raises(WorkOwnershipError, match="active owner"):
            ProductFactoryWorkOwnership(store, clock=clock).acquire(
                project_id=queued.project_id,
                work_id=queued.work_id,
                owner_id="program-host:queued-dispatch-competitor",
                lease_seconds=3,
            )

        release_first.set()
        outcomes = await asyncio.wait_for(task, timeout=1.0)
        return queued, outcomes

    queued, outcomes = _run(scenario())
    assert len(outcomes) == 2
    assert sorted(item.component_id for item in worker.dispatch_calls) == [
        "component-0",
        "component-1",
    ]
    assert authority.current(
        project_id=queued.project_id,
        work_id=queued.work_id,
    ) is None


def test_recovery_queue_renews_exact_fence_while_waiting_for_semaphore(
    monkeypatch,
    tmp_path,
) -> None:
    store, _, binding, task_id, coordinator, _ = _setup(
        tmp_path,
        component_count=2,
        repository_count=2,
    )
    clock = _MutableOwnershipClock(datetime(2026, 9, 28, 11, 0, tzinfo=UTC))
    authority = ProductFactoryWorkOwnership(store, clock=clock)
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    class QueueBlockingWorker(FakeProgramWorker):
        async def dispatch(self, request):
            self.dispatch_calls.append(request)
            if len(self.dispatch_calls) == 1:
                first_started.set()
                await release_first.wait()
            return _envelope(request, len(self.dispatch_calls))

    worker = QueueBlockingWorker()
    host = ProductFactoryProgramHost(
        store,
        worker,
        ownership=authority,
        owner_id="program-host:queued-recovery",
        lease_seconds=3,
    )
    monkeypatch.setattr(program_host_module, "_lease_heartbeat_interval", lambda _: 0.01)

    requests = tuple(
        coordinator.start(component_id)
        for component_id in ("component-0", "component-1")
    )
    seed_leases = tuple(host._acquire(request) for request in requests)
    try:
        host._checkpoint_running(
            host_task_id=task_id,
            binding=binding,
            coordinator=coordinator,
            requests=requests,
            leases=seed_leases,
        )
    finally:
        for lease in seed_leases:
            host._release_best_effort(lease)

    async def scenario():
        task = asyncio.create_task(
            host.recover_running(
                host_task_id=task_id,
                binding=binding,
                coordinator=coordinator,
                max_parallel=1,
            )
        )
        await first_started.wait()
        first_component = worker.dispatch_calls[0].component_id
        first_request = next(
            request for request in requests if request.component_id == first_component
        )
        queued = next(
            request for request in requests if request.component_id != first_component
        )
        for _ in range(100):
            original = authority.current(
                project_id=queued.project_id,
                work_id=queued.work_id,
            )
            first_original = authority.current(
                project_id=first_request.project_id,
                work_id=first_request.work_id,
            )
            if original is not None and first_original is not None:
                break
            await asyncio.sleep(0)
        else:
            raise AssertionError("recovery leases were not acquired")

        clock.advance(seconds=2)
        for _ in range(200):
            refreshed = authority.current(
                project_id=queued.project_id,
                work_id=queued.work_id,
            )
            first_refreshed = authority.current(
                project_id=first_request.project_id,
                work_id=first_request.work_id,
            )
            if (
                refreshed is not None
                and first_refreshed is not None
                and refreshed.expires_at > original.expires_at
                and first_refreshed.expires_at > first_original.expires_at
            ):
                break
            await asyncio.sleep(0.002)
        else:
            raise AssertionError("recovery lease heartbeats did not extend both exact fences")

        assert refreshed.fence == original.fence
        assert first_refreshed.fence == first_original.fence
        clock.advance(seconds=2)
        with pytest.raises(WorkOwnershipError, match="active owner"):
            ProductFactoryWorkOwnership(store, clock=clock).acquire(
                project_id=queued.project_id,
                work_id=queued.work_id,
                owner_id="program-host:queued-recovery-competitor",
                lease_seconds=3,
            )

        release_first.set()
        outcomes = await asyncio.wait_for(task, timeout=1.0)
        return queued, outcomes

    queued, outcomes = _run(scenario())
    assert len(outcomes) == 2
    assert sorted(item.component_id for item in worker.dispatch_calls) == [
        "component-0",
        "component-1",
    ]
    assert all(
        IdempotencyLedger(store).require(f"pf-worker:{request.work_id}").status
        is IdempotencyStatus.COMPLETED
        for request in requests
    )
    assert authority.current(
        project_id=queued.project_id,
        work_id=queued.work_id,
    ) is None


def test_result_reconcile_marker_failure_reports_actual_pending_ledger_status(tmp_path) -> None:
    store, _, binding, task_id, coordinator, _ = _setup(tmp_path)

    class FailAfterReservationOwnership(ProductFactoryWorkOwnership):
        def __init__(self, target_store):
            super().__init__(target_store)
            self.assert_calls = 0

        def assert_owner_in_transaction(self, connection, **kwargs):
            self.assert_calls += 1
            if self.assert_calls >= 3:
                raise WorkOwnershipError("forced post-effect fence loss")
            return super().assert_owner_in_transaction(connection, **kwargs)

    authority = FailAfterReservationOwnership(store)
    worker = FakeProgramWorker()
    host = ProductFactoryProgramHost(
        store,
        worker,
        ownership=authority,
        owner_id="program-host:status-truth",
    )

    outcomes = _run(
        host.dispatch_ready(
            host_task_id=task_id,
            binding=binding,
            coordinator=coordinator,
            max_count=1,
        )
    )

    request = _record(coordinator, "component-0").request
    durable = IdempotencyLedger(store).require(f"pf-worker:{request.work_id}")
    assert durable.status is IdempotencyStatus.PENDING
    assert outcomes[0].disposition is ProgramWorkDisposition.UNCERTAIN
    assert outcomes[0].operation_status is IdempotencyStatus.PENDING
    assert "uncertainty marker failed" in outcomes[0].detail


def test_heartbeat_authority_loss_cancels_inflight_effect_and_marks_uncertain(
    monkeypatch,
    tmp_path,
) -> None:
    store, _, binding, task_id, coordinator, _ = _setup(tmp_path)
    cancelled = asyncio.Event()

    class LoseHeartbeatOwnership(ProductFactoryWorkOwnership):
        def __init__(self, target_store):
            super().__init__(target_store)
            self.renew_calls = 0

        def renew(self, **kwargs):
            self.renew_calls += 1
            if self.renew_calls >= 2:
                raise WorkOwnershipError("forced heartbeat authority loss")
            return super().renew(**kwargs)

    class CancellableWorker(FakeProgramWorker):
        async def dispatch(self, request):
            self.dispatch_calls.append(request)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    authority = LoseHeartbeatOwnership(store)
    worker = CancellableWorker()
    host = ProductFactoryProgramHost(
        store,
        worker,
        ownership=authority,
        owner_id="program-host:heartbeat-loss",
        lease_seconds=3,
    )
    monkeypatch.setattr(program_host_module, "_lease_heartbeat_interval", lambda _: 0.001)

    outcomes = _run(
        host.dispatch_ready(
            host_task_id=task_id,
            binding=binding,
            coordinator=coordinator,
            max_count=1,
        )
    )

    request = _record(coordinator, "component-0").request
    assert cancelled.is_set()
    assert authority.renew_calls >= 2
    assert outcomes[0].disposition is ProgramWorkDisposition.UNCERTAIN
    assert outcomes[0].operation_status is IdempotencyStatus.UNCERTAIN
    assert IdempotencyLedger(store).require(
        f"pf-worker:{request.work_id}"
    ).status is IdempotencyStatus.UNCERTAIN


def test_heartbeat_loss_does_not_wait_forever_for_cancellation_resistant_worker(
    monkeypatch,
    tmp_path,
) -> None:
    store, _, binding, task_id, coordinator, _ = _setup(tmp_path)
    first_cancel_seen = asyncio.Event()
    release_worker = asyncio.Event()

    class LoseHeartbeatOwnership(ProductFactoryWorkOwnership):
        def __init__(self, target_store):
            super().__init__(target_store)
            self.renew_calls = 0

        def renew(self, **kwargs):
            self.renew_calls += 1
            if self.renew_calls >= 2:
                raise WorkOwnershipError("forced heartbeat authority loss")
            return super().renew(**kwargs)

    class CancellationResistantWorker(FakeProgramWorker):
        async def dispatch(self, request):
            self.dispatch_calls.append(request)
            try:
                await release_worker.wait()
            except asyncio.CancelledError:
                first_cancel_seen.set()
                await release_worker.wait()
            return _envelope(request)

    authority = LoseHeartbeatOwnership(store)
    worker = CancellationResistantWorker()
    host = ProductFactoryProgramHost(
        store,
        worker,
        ownership=authority,
        owner_id="program-host:bounded-cancel",
        lease_seconds=3,
    )
    monkeypatch.setattr(program_host_module, "_lease_heartbeat_interval", lambda _: 0.001)
    monkeypatch.setattr(program_host_module, "_EFFECT_CANCEL_GRACE_SECONDS", 0.01)

    async def scenario():
        outcomes = await asyncio.wait_for(
            host.dispatch_ready(
                host_task_id=task_id,
                binding=binding,
                coordinator=coordinator,
                max_count=1,
            ),
            timeout=0.5,
        )
        assert first_cancel_seen.is_set()
        release_worker.set()
        await asyncio.sleep(0)
        return outcomes

    outcomes = _run(scenario())

    request = _record(coordinator, "component-0").request
    durable = IdempotencyLedger(store).require(f"pf-worker:{request.work_id}")
    assert outcomes[0].disposition is ProgramWorkDisposition.UNCERTAIN
    assert outcomes[0].operation_status is IdempotencyStatus.UNCERTAIN
    assert durable.status is IdempotencyStatus.UNCERTAIN
    assert _record(coordinator, "component-0").state is WorkState.RUNNING


def test_unexpected_heartbeat_failure_cancels_inflight_effect_and_marks_uncertain(
    monkeypatch,
    tmp_path,
) -> None:
    store, _, binding, task_id, coordinator, _ = _setup(tmp_path)
    cancelled = asyncio.Event()

    class CrashHeartbeatOwnership(ProductFactoryWorkOwnership):
        def __init__(self, target_store):
            super().__init__(target_store)
            self.renew_calls = 0

        def renew(self, **kwargs):
            self.renew_calls += 1
            if self.renew_calls >= 2:
                raise RuntimeError("forced unexpected heartbeat backend failure")
            return super().renew(**kwargs)

    class CancellableWorker(FakeProgramWorker):
        async def dispatch(self, request):
            self.dispatch_calls.append(request)
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

    authority = CrashHeartbeatOwnership(store)
    worker = CancellableWorker()
    host = ProductFactoryProgramHost(
        store,
        worker,
        ownership=authority,
        owner_id="program-host:heartbeat-backend-failure",
        lease_seconds=3,
    )
    monkeypatch.setattr(program_host_module, "_lease_heartbeat_interval", lambda _: 0.001)

    outcomes = _run(
        host.dispatch_ready(
            host_task_id=task_id,
            binding=binding,
            coordinator=coordinator,
            max_count=1,
        )
    )

    request = _record(coordinator, "component-0").request
    assert cancelled.is_set()
    assert authority.renew_calls >= 2
    assert outcomes[0].disposition is ProgramWorkDisposition.UNCERTAIN
    assert outcomes[0].operation_status is IdempotencyStatus.UNCERTAIN
    assert IdempotencyLedger(store).require(
        f"pf-worker:{request.work_id}"
    ).status is IdempotencyStatus.UNCERTAIN


def _seed_durable_result_pending_for_reconcile(store, binding, task_id, coordinator):
    host = ProductFactoryProgramHost(store, FakeProgramWorker())
    request = coordinator.start("component-0")
    lease = host._acquire(request)
    try:
        host._checkpoint_running(
            host_task_id=task_id,
            binding=binding,
            coordinator=coordinator,
            requests=(request,),
            leases=(lease,),
        )
        operation, created = host._reserve_effect(
            host_task_id=task_id,
            request=request,
            lease=lease,
        )
        assert created is True
        assert operation.status is IdempotencyStatus.PENDING
        updated = coordinator.record_result(_envelope(request))
        assert updated.state is WorkState.REVIEW_REQUIRED
        host._save_fenced(task_id, binding, coordinator, lease)
    finally:
        host._release_best_effort(lease)
    operation_key = f"pf-worker:{request.work_id}"
    assert IdempotencyLedger(store).require(operation_key).status is IdempotencyStatus.PENDING
    return host, operation_key


def test_reconcile_durable_results_preserves_concurrent_terminal_completion(tmp_path) -> None:
    store, _, binding, task_id, coordinator, _ = _setup(tmp_path)
    host, operation_key = _seed_durable_result_pending_for_reconcile(
        store,
        binding,
        task_id,
        coordinator,
    )
    external_result = {"manual_reconciliation": "external completion is authoritative"}

    class CompleteOnFirstReadLedger(IdempotencyLedger):
        def __init__(self, ledger_store) -> None:
            super().__init__(ledger_store)
            self.triggered = False

        def get(self, key):
            record = super().get(key)
            if not self.triggered and key == operation_key:
                self.triggered = True
                IdempotencyLedger(store).complete(key, external_result)
            return record

    host._ledger = CompleteOnFirstReadLedger(store)

    assert host.reconcile_durable_results(
        host_task_id=task_id,
        coordinator=coordinator,
    ) == ()

    durable = IdempotencyLedger(store).require(operation_key)
    assert durable.status is IdempotencyStatus.COMPLETED
    assert durable.result == external_result


def test_reconcile_durable_results_rejects_concurrent_operation_rebinding(tmp_path) -> None:
    store, _, binding, task_id, coordinator, _ = _setup(tmp_path)
    host, operation_key = _seed_durable_result_pending_for_reconcile(
        store,
        binding,
        task_id,
        coordinator,
    )
    foreign_task = TaskQueue(store).create(
        workspace_id="ws-foreign",
        agent_id="foreign-worker",
        payload={"kind": "foreign"},
    )
    foreign_task_id = foreign_task.task_id
    foreign_fingerprint = "f" * 64

    class RebindOnFirstReadLedger(IdempotencyLedger):
        def __init__(self, ledger_store) -> None:
            super().__init__(ledger_store)
            self.triggered = False

        def get(self, key):
            record = super().get(key)
            if not self.triggered and key == operation_key:
                self.triggered = True
                replacement = IdempotencyLedger(store)
                replacement.release_pending(key)
                replacement.reserve_once(
                    operation_key=key,
                    task_id=foreign_task_id,
                    operation_type="foreign.effect",
                    input_fingerprint=foreign_fingerprint,
                )
            return record

    host._ledger = RebindOnFirstReadLedger(store)

    with pytest.raises(ProductFactoryProgramError, match="identity changed"):
        host.reconcile_durable_results(
            host_task_id=task_id,
            coordinator=coordinator,
        )

    durable = IdempotencyLedger(store).require(operation_key)
    assert durable.task_id == foreign_task_id
    assert durable.operation_type == "foreign.effect"
    assert durable.input_fingerprint == foreign_fingerprint
    assert durable.status is IdempotencyStatus.PENDING
    assert durable.result is None

