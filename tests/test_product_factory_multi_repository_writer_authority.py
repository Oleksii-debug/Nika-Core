from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_checkpoint_host import ProductFactoryCheckpointHost
from nika_core.product_factory_coordinator import ComponentWorkRequest, WorkerResultEnvelope
from nika_core.product_factory_multi_repository import MultiRepositoryProductFactoryHost
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from nika_core.product_project import (
    ProductProjectRepository,
    ProductProjectSpec,
    ProductRequirement,
)
from nika_core.toolsmith.contracts import (
    CodingResult,
    RecoveryState,
    WorkerFailure,
    WorkerFailureKind,
)

_GRAPH_STAGE = "product_factory.repository_graph.v1"
_LINEAGE_STAGE = "product_factory.repair_lineage.v1"
_PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})


class _NoopProgramWorker:
    async def dispatch(self, request: ComponentWorkRequest) -> WorkerResultEnvelope:
        raise AssertionError(f"unexpected dispatch: {request.work_id}")

    async def inspect(self, work_id: str) -> RecoveryState | None:
        del work_id
        return None

    async def recover(
        self,
        request: ComponentWorkRequest,
        state: RecoveryState,
    ) -> WorkerResultEnvelope:
        raise AssertionError(f"unexpected recovery: {request.work_id}:{state.phase}")


class _TracingSQLiteStore(SQLiteStore):
    def __init__(self, path: Path | str) -> None:
        super().__init__(path)
        self.traces: list[tuple[str, ...]] = []

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        trace: list[str] = []
        with super().connection() as conn:
            conn.set_trace_callback(trace.append)
            try:
                yield conn
            finally:
                self.traces.append(tuple(trace))


def _create_state(tmp_path: Path):
    store = _TracingSQLiteStore(tmp_path / "writer-authority.db")
    store.initialize()
    graph = ProductRepositoryGraph(
        project_id="project:writer-authority",
        repositories=(RepositoryRef("repo:a", "github", "owner/a", "main"),),
        components=(ProductComponent("a", "repo:a", ("src/a",)),),
    )
    project = ProductProjectRepository(store).create(
        project_id=graph.project_id,
        name="Writer Authority",
        spec=ProductProjectSpec(
            goal="Serialize Product Factory authority publication",
            desired_outcome="Concurrent publishers cannot pass the same stale pre-read",
            requirements=(
                ProductRequirement(
                    requirement_id="writer-authority",
                    text="Durable authority publication is one serialized read-check-write",
                    acceptance=("Writer transaction precedes authoritative pre-read",),
                ),
            ),
            repository_refs=("owner/a",),
        ),
        idempotency_key="create:writer-authority",
    )
    task = TaskQueue(store).create(
        workspace_id="ws:writer-authority",
        agent_id="product-factory",
        payload={
            "kind": "product_factory",
            "product_project_id": project.project_id,
        },
    )
    host = MultiRepositoryProductFactoryHost(store, _NoopProgramWorker())
    state = host.initialize(
        host_task_id=task.task_id,
        project=project,
        graph=graph,
        graph_version=1,
        base_shas={"repo:a": "a" * 40},
        component_goals={"a": "Implement A"},
        permission_ceiling=_PERMISSIONS,
    )
    return store, task.task_id, host, state


def _assert_immediate_before_stage_read(
    store: _TracingSQLiteStore,
    stage: str,
) -> None:
    candidates = [
        trace
        for trace in store.traces
        if any(stage in statement for statement in trace)
        and any("INSERT INTO checkpoints" in statement for statement in trace)
    ]
    assert candidates, f"no durable writer trace found for {stage}"
    trace = candidates[-1]
    begin_index = next(
        index
        for index, statement in enumerate(trace)
        if statement.strip().upper().startswith("BEGIN IMMEDIATE")
    )
    read_index = next(
        index
        for index, statement in enumerate(trace)
        if "FROM checkpoints" in statement and stage in statement
    )
    assert begin_index < read_index


def test_graph_authority_serializes_before_authoritative_pre_read(tmp_path: Path) -> None:
    store, _, _, _ = _create_state(tmp_path)

    _assert_immediate_before_stage_read(store, _GRAPH_STAGE)


def test_repair_lineage_serializes_before_generation_pre_read(tmp_path: Path) -> None:
    store, task_id, host, state = _create_state(tmp_path)
    request = state.coordinator.start("a")
    state.coordinator.record_result(
        WorkerResultEnvelope(
            work_id=request.work_id,
            component_id=request.component_id,
            repository_id=request.repository_id,
            base_sha=request.base_sha,
            result_sha="1" * 40,
            diff_digest="2" * 64,
            coding_result=CodingResult(
                job_id=request.work_id,
                failure=WorkerFailure(
                    WorkerFailureKind.PROCESS_FAILED,
                    "deterministic failure for writer-authority regression",
                    retryable=True,
                ),
            ),
        )
    )
    ProductFactoryCheckpointHost(store).save(
        host_task_id=task_id,
        checkpoint=state.binding.checkpoint(state.coordinator),
    )
    store.traces.clear()

    host.prepare_repair_and_checkpoint(
        host_task_id=task_id,
        state=state,
        component_id="a",
        reason="retry from exact failed result",
    )

    _assert_immediate_before_stage_read(store, _LINEAGE_STAGE)
