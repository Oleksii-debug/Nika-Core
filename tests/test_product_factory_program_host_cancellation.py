from __future__ import annotations

import asyncio

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_checkpoint_host import ProductFactoryCheckpointHost
from nika_core.product_factory_coordinator import (
    WorkerResultEnvelope,
    WorkState,
)
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from nika_core.product_factory_program_host import (
    ProductFactoryProgramHost,
    ProgramWorkDisposition,
)
from nika_core.product_factory_project_binding import ProductProjectCoordinatorBinding
from nika_core.product_project import (
    ProductProjectRepository,
    ProductProjectSpec,
    ProductRequirement,
)
from nika_core.runtime.idempotency import IdempotencyLedger, IdempotencyStatus
from nika_core.toolsmith.contracts import CodingResult, RecoveryState, TestEvidence

PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})
BASE_SHA_0 = "1" * 40
BASE_SHA_1 = "2" * 40
RESULT_SHA = "a" * 40
DIFF_DIGEST = "d" * 64
TEST_DIGEST = "e" * 64


def _graph() -> ProductRepositoryGraph:
    return ProductRepositoryGraph(
        project_id="project-cancel",
        repositories=(
            RepositoryRef("repo-0", "github", "org/cancel-0", "main"),
            RepositoryRef("repo-1", "github", "org/cancel-1", "main"),
        ),
        components=(
            ProductComponent(
                component_id="component-0",
                repository_id="repo-0",
                paths=("src/component-0",),
                test_commands=(("python", "-m", "pytest", "tests/component-0"),),
            ),
            ProductComponent(
                component_id="component-1",
                repository_id="repo-1",
                paths=("src/component-1",),
                test_commands=(("python", "-m", "pytest", "tests/component-1"),),
            ),
        ),
    )


def _setup(tmp_path):
    graph = _graph()
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    project = ProductProjectRepository(store).create(
        project_id=graph.project_id,
        name="Cancellation crash-window product",
        spec=ProductProjectSpec(
            goal="Prove cancellation-safe Product Factory dispatch",
            desired_outcome="No ambiguous worker side effect is replayed after restart",
            requirements=(
                ProductRequirement(
                    "req-cancel",
                    "Differentiate reserved and pre-dispatch work after cancellation",
                    ("No duplicate worker call after restart",),
                ),
            ),
            repository_refs=tuple(repository.locator for repository in graph.repositories),
        ),
        idempotency_key="create:project-cancel",
    )
    binding = ProductProjectCoordinatorBinding(project, graph)
    task = TaskQueue(store).create(
        workspace_id="ws-product",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": project.project_id},
    )
    coordinator = binding.plan(
        base_shas={"repo-0": BASE_SHA_0, "repo-1": BASE_SHA_1},
        component_goals={
            "component-0": "Implement component 0",
            "component-1": "Implement component 1",
        },
        permission_ceiling=PERMISSIONS,
    )
    ProductFactoryCheckpointHost(store).save(
        host_task_id=task.task_id,
        checkpoint=binding.checkpoint(coordinator),
    )
    return store, binding, task.task_id, coordinator, graph


def _envelope(request, ordinal: int) -> WorkerResultEnvelope:
    return WorkerResultEnvelope(
        work_id=request.work_id,
        component_id=request.component_id,
        repository_id=request.repository_id,
        base_sha=request.base_sha,
        result_sha=f"{ordinal:040x}"[-40:] if ordinal else RESULT_SHA,
        diff_digest=f"{ordinal:064x}"[-64:] if ordinal else DIFF_DIGEST,
        coding_result=CodingResult(
            job_id=request.work_id,
            test_evidence=(
                TestEvidence(
                    ("python", "-m", "pytest", request.component_id),
                    0,
                    TEST_DIGEST,
                ),
            ),
        ),
    )


def _record(coordinator, component_id: str):
    return next(
        record
        for record in coordinator.snapshot().records
        if record.request.component_id == component_id
    )


class BlockingWorker:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.dispatch_calls = []

    async def dispatch(self, request):
        self.dispatch_calls.append(request)
        self.started.set()
        await self.release.wait()
        return _envelope(request, 100)

    async def inspect(self, work_id):
        raise AssertionError(f"unexpected inspect before restart: {work_id}")

    async def recover(self, request, state):
        raise AssertionError(f"unexpected recovery before restart: {request.work_id}: {state}")


class RecoveryWorker:
    def __init__(self, uncertain_work_id: str) -> None:
        self.uncertain_work_id = uncertain_work_id
        self.dispatch_calls = []
        self.inspect_calls = []
        self.recover_calls = []

    async def dispatch(self, request):
        self.dispatch_calls.append(request)
        return _envelope(request, 201)

    async def inspect(self, work_id):
        self.inspect_calls.append(work_id)
        if work_id != self.uncertain_work_id:
            raise AssertionError(f"pre-dispatch work must not be inspected: {work_id}")
        return RecoveryState("interrupted", "resume-after-cancel")

    async def recover(self, request, state):
        self.recover_calls.append((request, state))
        assert request.work_id == self.uncertain_work_id
        return _envelope(request, 202)


def test_cancelled_dispatch_keeps_reserved_work_uncertain_and_pre_dispatch_work_unreserved(
    tmp_path,
) -> None:
    store, binding, task_id, coordinator, graph = _setup(tmp_path)

    async def cancel_during_first_worker_side_effect():
        worker = BlockingWorker()
        host = ProductFactoryProgramHost(store, worker)
        pending = asyncio.create_task(
            host.dispatch_ready(
                host_task_id=task_id,
                binding=binding,
                coordinator=coordinator,
                max_parallel=1,
                max_count=2,
            )
        )
        await worker.started.wait()

        first = worker.dispatch_calls[0]
        second = next(
            record.request
            for record in coordinator.snapshot().records
            if record.request.work_id != first.work_id
        )
        ledger = IdempotencyLedger(store)

        assert ledger.require(f"pf-worker:{first.work_id}").status is IdempotencyStatus.PENDING
        assert ledger.get(f"pf-worker:{second.work_id}") is None
        assert _record(coordinator, first.component_id).state is WorkState.RUNNING
        assert _record(coordinator, second.component_id).state is WorkState.RUNNING

        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending

        assert (
            ledger.require(f"pf-worker:{first.work_id}").status
            is IdempotencyStatus.UNCERTAIN
        )
        assert ledger.get(f"pf-worker:{second.work_id}") is None
        assert [request.work_id for request in worker.dispatch_calls] == [first.work_id]
        return first, second

    first, second = asyncio.run(cancel_during_first_worker_side_effect())

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    project = ProductProjectRepository(restarted_store).get(graph.project_id)
    restarted_binding = ProductProjectCoordinatorBinding(project, graph)
    recovery_worker = RecoveryWorker(first.work_id)
    restarted_host = ProductFactoryProgramHost(restarted_store, recovery_worker)
    restored = restarted_host.restore_latest(
        host_task_id=task_id,
        binding=restarted_binding,
    )

    outcomes = asyncio.run(
        restarted_host.recover_running(
            host_task_id=task_id,
            binding=restarted_binding,
            coordinator=restored,
            max_parallel=1,
        )
    )

    assert recovery_worker.inspect_calls == [first.work_id]
    assert [request.work_id for request, _state in recovery_worker.recover_calls] == [
        first.work_id
    ]
    assert [request.work_id for request in recovery_worker.dispatch_calls] == [second.work_id]
    assert {outcome.disposition for outcome in outcomes} == {
        ProgramWorkDisposition.REVIEW_REQUIRED
    }

    ledger = IdempotencyLedger(restarted_store)
    assert ledger.require(f"pf-worker:{first.work_id}").status is IdempotencyStatus.COMPLETED
    assert ledger.require(f"pf-worker:{second.work_id}").status is IdempotencyStatus.COMPLETED
