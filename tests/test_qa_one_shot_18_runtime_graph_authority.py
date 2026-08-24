from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_coordinator import (
    ComponentWorkRequest,
    ReviewDecision,
    WorkerResultEnvelope,
)
from nika_core.product_factory_multi_repository import (
    MultiRepositoryExecutionError,
    MultiRepositoryProductFactoryHost,
)
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
from nika_core.toolsmith.contracts import CodingResult, RecoveryState, TestEvidence

_PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})


class _SuccessfulProgramWorker:
    async def dispatch(self, request: ComponentWorkRequest) -> WorkerResultEnvelope:
        return WorkerResultEnvelope(
            work_id=request.work_id,
            component_id=request.component_id,
            repository_id=request.repository_id,
            base_sha=request.base_sha,
            result_sha="1" * 40,
            diff_digest="2" * 64,
            coding_result=CodingResult(
                job_id=request.work_id,
                test_evidence=tuple(
                    TestEvidence(
                        command=command,
                        exit_code=0,
                        output_digest="3" * 64,
                    )
                    for command in request.acceptance_commands
                ),
            ),
        )

    async def inspect(self, work_id: str) -> RecoveryState | None:
        del work_id
        return None

    async def recover(
        self,
        request: ComponentWorkRequest,
        state: RecoveryState,
    ) -> WorkerResultEnvelope:
        raise AssertionError(f"unexpected recovery: {request.work_id}:{state.phase}")


def _graph() -> ProductRepositoryGraph:
    return ProductRepositoryGraph(
        project_id="project:runtime-graph-authority",
        repositories=(
            RepositoryRef("repo:a", "github", "owner/a", "main"),
            RepositoryRef("repo:b", "github", "owner/b", "main"),
            RepositoryRef("repo:c", "github", "owner/c", "main"),
        ),
        components=(
            ProductComponent(
                "a",
                "repo:a",
                ("src/a",),
                test_commands=(("python", "-m", "pytest", "tests/a"),),
            ),
            ProductComponent(
                "b",
                "repo:b",
                ("src/b",),
                dependencies=("a",),
                test_commands=(("python", "-m", "pytest", "tests/b"),),
            ),
            ProductComponent(
                "c",
                "repo:c",
                ("src/c",),
                dependencies=("b",),
                test_commands=(("python", "-m", "pytest", "tests/c"),),
            ),
        ),
    )


def test_runtime_graph_dependency_rewrite_cannot_bypass_durable_authority(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "runtime-graph-authority.db")
    store.initialize()
    graph = _graph()
    project = ProductProjectRepository(store).create(
        project_id=graph.project_id,
        name="Runtime Graph Authority",
        spec=ProductProjectSpec(
            goal="Keep repository dependency authority immutable after durable binding",
            desired_outcome="A runtime graph rewrite fails before downstream readiness changes",
            requirements=(
                ProductRequirement(
                    requirement_id="runtime-graph-authority",
                    text="Durable repository dependencies remain authoritative in memory",
                    acceptance=("Dependency rewrite fails closed",),
                ),
            ),
            repository_refs=tuple(item.locator for item in graph.repositories),
        ),
        idempotency_key="create:runtime-graph-authority",
    )
    task = TaskQueue(store).create(
        workspace_id="ws:runtime-graph-authority",
        agent_id="product-factory",
        payload={
            "kind": "product_factory",
            "product_project_id": project.project_id,
        },
    )
    host = MultiRepositoryProductFactoryHost(store, _SuccessfulProgramWorker())
    state = host.initialize(
        host_task_id=task.task_id,
        project=project,
        graph=graph,
        graph_version=1,
        base_shas={"repo:a": "a" * 40, "repo:b": "b" * 40, "repo:c": "c" * 40},
        component_goals={"a": "Implement A", "b": "Implement B", "c": "Implement C"},
        permission_ceiling=_PERMISSIONS,
    )

    outcomes = asyncio.run(
        host.dispatch_ready(
            host_task_id=task.task_id,
            state=state,
            max_parallel=1,
            max_count=1,
        )
    )
    assert [item.component_id for item in outcomes] == ["a"]

    original_components = state.authority.graph.components
    state.authority.graph.components = tuple(
        ProductComponent(
            component.component_id,
            component.repository_id,
            component.paths,
            dependencies=(() if component.component_id == "c" else component.dependencies),
            build_commands=component.build_commands,
            test_commands=component.test_commands,
            release_identity=component.release_identity,
        )
        for component in original_components
    )

    with pytest.raises(MultiRepositoryExecutionError, match="repository graph authority"):
        host.review_and_checkpoint(
            host_task_id=task.task_id,
            state=state,
            component_id="a",
            decision=ReviewDecision(
                reviewer_id="independent-qa:runtime-graph-authority",
                accepted=True,
                reason="attempt A has exact successful evidence",
                evidence_refs=("qa:runtime-graph-authority",),
            ),
        )
