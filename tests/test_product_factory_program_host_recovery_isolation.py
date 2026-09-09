from __future__ import annotations

import asyncio

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_checkpoint_host import ProductFactoryCheckpointHost
from nika_core.product_factory_coordinator import WorkerResultEnvelope, WorkState
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


def _graph() -> ProductRepositoryGraph:
    return ProductRepositoryGraph(
        project_id="project-recovery-isolation",
        repositories=(
            RepositoryRef(
                repository_id="repo-0",
                provider="github",
                locator="org/recovery-isolation",
                default_branch="main",
            ),
        ),
        components=tuple(
            ProductComponent(
                component_id=f"component-{index}",
                repository_id="repo-0",
                paths=(f"src/component-{index}",),
                dependencies=(),
                test_commands=(("python", "-m", "pytest", f"tests/component-{index}"),),
            )
            for index in range(2)
        ),
    )


def _setup(tmp_path):
    graph = _graph()
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    projects = ProductProjectRepository(store)
    project = projects.create(
        project_id=graph.project_id,
        name="Recovery isolation",
        spec=ProductProjectSpec(
            goal="Recover independent coding work",
            desired_outcome="One broken inspection cannot stop sibling recovery",
            requirements=(
                ProductRequirement(
                    "req-1",
                    "Recovery is component isolated",
                    ("Independent RUNNING siblings continue recovery",),
                ),
            ),
            repository_refs=("org/recovery-isolation",),
        ),
        idempotency_key="create:recovery-isolation",
    )
    binding = ProductProjectCoordinatorBinding(project, graph)
    task = TaskQueue(store).create(
        workspace_id="ws-recovery-isolation",
        agent_id="product-factory",
        payload={"kind": "product_factory", "product_project_id": project.project_id},
    )
    coordinator = binding.plan(
        base_shas={"repo-0": "a" * 40},
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
    return store, binding, task.task_id, coordinator


def _record(coordinator, component_id: str):
    return next(
        record
        for record in coordinator.snapshot().records
        if record.request.component_id == component_id
    )


class FailingDispatchWorker:
    async def dispatch(self, request):
        raise RuntimeError(f"transport lost for {request.work_id}")

    async def inspect(self, work_id):
        raise AssertionError(f"unexpected inspect during dispatch: {work_id}")

    async def recover(self, request, state):
        raise AssertionError(f"unexpected recovery during dispatch: {request.work_id}")


class PartiallyBrokenRecoveryWorker:
    def __init__(self, broken_work_id: str, recoverable_work_id: str) -> None:
        self.broken_work_id = broken_work_id
        self.recoverable_work_id = recoverable_work_id
        self.inspect_calls: list[str] = []
        self.recover_calls: list[str] = []

    async def dispatch(self, request):
        raise AssertionError(f"duplicate dispatch forbidden: {request.work_id}")

    async def inspect(self, work_id):
        self.inspect_calls.append(work_id)
        if work_id == self.broken_work_id:
            raise OSError("worker inspection transport unavailable")
        assert work_id == self.recoverable_work_id
        return RecoveryState("interrupted", "resume-sibling")

    async def recover(self, request, state):
        self.recover_calls.append(request.work_id)
        assert request.work_id == self.recoverable_work_id
        assert state.opaque_token == "resume-sibling"
        return WorkerResultEnvelope(
            work_id=request.work_id,
            component_id=request.component_id,
            repository_id=request.repository_id,
            base_sha=request.base_sha,
            result_sha="b" * 40,
            diff_digest="d" * 64,
            coding_result=CodingResult(
                job_id=request.work_id,
                test_evidence=(
                    TestEvidence(
                        request.acceptance_commands[0],
                        0,
                        "recovered-tests-ok",
                    ),
                ),
            ),
        )


def test_inspect_failure_marks_only_affected_operation_uncertain_and_recovers_sibling(
    tmp_path,
) -> None:
    store, binding, task_id, coordinator = _setup(tmp_path)
    first_host = ProductFactoryProgramHost(store, FailingDispatchWorker())
    first_outcomes = asyncio.run(
        first_host.dispatch_ready(
            host_task_id=task_id,
            binding=binding,
            coordinator=coordinator,
            max_parallel=2,
            max_count=2,
        )
    )
    assert {item.disposition for item in first_outcomes} == {
        ProgramWorkDisposition.UNCERTAIN
    }

    broken = _record(coordinator, "component-0").request
    sibling = _record(coordinator, "component-1").request

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    project = ProductProjectRepository(restarted_store).get("project-recovery-isolation")
    restarted_binding = ProductProjectCoordinatorBinding(project, _graph())
    worker = PartiallyBrokenRecoveryWorker(broken.work_id, sibling.work_id)
    restarted_host = ProductFactoryProgramHost(restarted_store, worker)
    restored = restarted_host.restore_latest(
        host_task_id=task_id,
        binding=restarted_binding,
    )

    outcomes = asyncio.run(
        restarted_host.recover_running(
            host_task_id=task_id,
            binding=restarted_binding,
            coordinator=restored,
            max_parallel=2,
        )
    )

    by_component = {item.component_id: item for item in outcomes}
    assert by_component["component-0"].disposition is ProgramWorkDisposition.UNCERTAIN
    assert by_component["component-0"].operation_status is IdempotencyStatus.UNCERTAIN
    assert by_component["component-0"].state is WorkState.RUNNING
    assert "OSError" in by_component["component-0"].detail

    assert by_component["component-1"].disposition is ProgramWorkDisposition.REVIEW_REQUIRED
    assert by_component["component-1"].operation_status is IdempotencyStatus.COMPLETED
    assert by_component["component-1"].state is WorkState.REVIEW_REQUIRED
    assert set(worker.inspect_calls) == {broken.work_id, sibling.work_id}
    assert worker.recover_calls == [sibling.work_id]

    ledger = IdempotencyLedger(restarted_store)
    assert ledger.require(f"pf-worker:{broken.work_id}").status is IdempotencyStatus.UNCERTAIN
    assert ledger.require(f"pf-worker:{sibling.work_id}").status is IdempotencyStatus.COMPLETED
