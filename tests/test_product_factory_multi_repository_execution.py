from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_coordinator import (
    ComponentWorkRequest,
    ReviewDecision,
    WorkerResultEnvelope,
    WorkState,
)
from nika_core.product_factory_multi_repository import (
    MultiRepositoryProductFactoryHost,
)
from nika_core.product_factory_orchestration import (
    ComponentBrief,
    DynamicTeamComposer,
    ProductComponent,
    ProductRepositoryGraph,
    ProjectScale,
    RepositoryRef,
    TeamCompositionRequest,
)
from nika_core.product_factory_program_host import ProgramWorkDisposition
from nika_core.product_factory_review_authority import (
    ProductFactoryReviewSubject,
    reviewer_principal_bindings_ref,
    team_plan_fingerprint_ref,
)
from nika_core.product_project import ProductProjectRepository, ProductProjectSpec
from nika_core.toolsmith.contracts import (
    CodingResult,
    RecoveryState,
    TestEvidence,
    WorkerFailure,
    WorkerFailureKind,
)

PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})
GRAPH_VERSION = 7
REVIEWER_ACTOR = "reviewer:qa"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha(value: str) -> str:
    return _digest(value)[:40]


class TrustedReviewEvidence:
    def verify(
        self,
        subject: ProductFactoryReviewSubject,
        evidence_refs: tuple[str, ...],
    ) -> bool:
        return (
            subject.reviewer_id == REVIEWER_ACTOR
            and subject.producer_actor_id.startswith("builder:")
            and evidence_refs == (f"trusted-review:{subject.component_id}",)
        )


class DeterministicProgramWorker:
    def __init__(self) -> None:
        self.active = 0
        self.peak_active = 0
        self.executions: dict[str, int] = {}
        self.fail_first = {"assets"}

    async def dispatch(self, request: ComponentWorkRequest) -> WorkerResultEnvelope:
        component_id = request.component_id
        ordinal = self.executions.get(component_id, 0) + 1
        self.executions[component_id] = ordinal
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        try:
            await asyncio.sleep(0.01)
            if component_id in self.fail_first and ordinal == 1:
                coding_result = CodingResult(
                    job_id=request.work_id,
                    failure=WorkerFailure(
                        WorkerFailureKind.PROCESS_FAILED,
                        "deterministic first-attempt failure",
                        retryable=True,
                    ),
                )
            else:
                coding_result = CodingResult(
                    job_id=request.work_id,
                    test_evidence=tuple(
                        TestEvidence(
                            command=command,
                            exit_code=0,
                            output_digest=_digest(
                                f"test:{request.work_id}:{' '.join(command)}"
                            ),
                        )
                        for command in request.acceptance_commands
                    ),
                )
            return WorkerResultEnvelope(
                work_id=request.work_id,
                component_id=component_id,
                repository_id=request.repository_id,
                base_sha=request.base_sha,
                result_sha=_sha(f"result:{request.work_id}:{ordinal}"),
                diff_digest=_digest(f"diff:{request.work_id}:{ordinal}"),
                coding_result=coding_result,
                producer_actor_id=f"builder:{component_id}",
            )
        finally:
            self.active -= 1

    async def inspect(self, work_id: str) -> RecoveryState | None:
        del work_id
        return None

    async def recover(
        self,
        request: ComponentWorkRequest,
        state: RecoveryState,
    ) -> WorkerResultEnvelope:
        raise AssertionError(f"unexpected recovery: {request.work_id}:{state.phase}")


def _graph(project_id: str) -> ProductRepositoryGraph:
    repositories = (
        RepositoryRef("repo-api", "github", "owner/api", "main"),
        RepositoryRef("repo-assets", "github", "owner/assets", "main"),
        RepositoryRef("repo-sdk", "github", "owner/sdk", "main"),
        RepositoryRef("repo-desktop", "github", "owner/desktop", "main"),
    )
    components = (
        ProductComponent(
            "api",
            "repo-api",
            ("src/api",),
            test_commands=(("python", "-m", "pytest", "tests/api"),),
        ),
        ProductComponent(
            "assets",
            "repo-assets",
            ("src/assets",),
            test_commands=(("python", "-m", "pytest", "tests/assets"),),
        ),
        ProductComponent(
            "sdk",
            "repo-sdk",
            ("src/sdk",),
            dependencies=("api",),
            test_commands=(("python", "-m", "pytest", "tests/sdk"),),
        ),
        ProductComponent(
            "desktop",
            "repo-desktop",
            ("src/desktop",),
            dependencies=("assets", "sdk"),
            test_commands=(("python", "-m", "pytest", "tests/desktop"),),
        ),
    )
    return ProductRepositoryGraph(
        project_id=project_id,
        repositories=repositories,
        components=components,
    )


def _team(project_id: str, graph: ProductRepositoryGraph):
    plan = DynamicTeamComposer().compose(
        TeamCompositionRequest(
            project_id=project_id,
            components=tuple(
                ComponentBrief(component.component_id, "backend")
                for component in graph.components
            ),
            acceptance_criteria=(
                "Independent review is required",
                "The final Windows product is accessible",
            ),
            permission_ceiling=PERMISSIONS,
            scale=ProjectScale.LARGE,
        )
    )
    reviewer_roles = tuple(role for role in plan.roles if role.independent_review)
    assert len(reviewer_roles) == 1
    principals = ((reviewer_roles[0].role_id, REVIEWER_ACTOR),)
    return plan, principals


def _host(
    store: SQLiteStore,
    worker: DeterministicProgramWorker,
    *,
    team_plan,
    reviewer_principals,
) -> MultiRepositoryProductFactoryHost:
    return MultiRepositoryProductFactoryHost(
        store,
        worker,
        team_plan=team_plan,
        review_evidence_authority=TrustedReviewEvidence(),
        reviewer_principals=reviewer_principals,
    )


def _review(
    host: MultiRepositoryProductFactoryHost,
    state,
    *,
    task_id: str,
    component_id: str,
) -> None:
    host.review_and_checkpoint(
        host_task_id=task_id,
        state=state,
        component_id=component_id,
        decision=ReviewDecision(
            reviewer_id=REVIEWER_ACTOR,
            accepted=True,
            reason=f"{component_id} exact evidence accepted",
            evidence_refs=(f"trusted-review:{component_id}",),
        ),
    )


def _record(state, component_id: str):
    return next(
        record
        for record in state.coordinator.snapshot().records
        if record.request.component_id == component_id
    )


def test_multi_repository_execution_uses_current_trusted_review_and_restart_authority(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "multi repository trusted review.db")
    store.initialize()
    graph = _graph("product-multi-repo-current")
    team_plan, reviewer_principals = _team(graph.project_id, graph)
    repository = ProductProjectRepository(store)
    project = repository.create(
        project_id=graph.project_id,
        name="Current multi repository Product Factory",
        spec=ProductProjectSpec(
            goal="Build a dependency-ordered product across four repositories",
            desired_outcome="Every component reaches trusted independent acceptance",
            repository_refs=tuple(item.locator for item in graph.repositories),
            team_refs=(
                team_plan.plan_id,
                team_plan_fingerprint_ref(team_plan),
                reviewer_principal_bindings_ref(team_plan, reviewer_principals),
            ),
        ),
        idempotency_key="create:product-multi-repo-current",
    )
    task = TaskQueue(store).create(
        workspace_id="product.factory",
        agent_id="product-factory",
        payload={
            "kind": "product_factory",
            "product_project_id": project.project_id,
        },
    )
    worker = DeterministicProgramWorker()
    host = _host(
        store,
        worker,
        team_plan=team_plan,
        reviewer_principals=reviewer_principals,
    )
    state = host.initialize(
        host_task_id=task.task_id,
        project=project,
        graph=graph,
        graph_version=GRAPH_VERSION,
        base_shas={
            "repo-api": "a" * 40,
            "repo-assets": "b" * 40,
            "repo-sdk": "c" * 40,
            "repo-desktop": "d" * 40,
        },
        component_goals={
            component.component_id: f"Implement {component.component_id}"
            for component in graph.components
        },
        permission_ceiling=PERMISSIONS,
    )

    wave_one = asyncio.run(
        host.dispatch_ready(
            host_task_id=task.task_id,
            state=state,
            max_parallel=4,
        )
    )
    by_component = {item.component_id: item for item in wave_one}
    assert set(by_component) == {"api", "assets"}
    assert by_component["api"].disposition is ProgramWorkDisposition.REVIEW_REQUIRED
    assert by_component["assets"].disposition is ProgramWorkDisposition.REPAIR_REQUIRED
    assert worker.peak_active >= 2
    assert _record(state, "api").state is WorkState.REVIEW_REQUIRED
    assert _record(state, "assets").state is WorkState.REPAIR_REQUIRED
    failed_assets_sha = _record(state, "assets").result.result_sha

    _review(host, state, task_id=task.task_id, component_id="api")
    assert _record(state, "sdk").state is WorkState.READY
    assert _record(state, "desktop").state is WorkState.PLANNED

    repair_request, lineage = host.prepare_repair_and_checkpoint(
        host_task_id=task.task_id,
        state=state,
        component_id="assets",
        reason="repair deterministic asset failure",
    )
    assert repair_request.attempt == 2
    assert repair_request.base_sha == failed_assets_sha
    assert lineage.from_result_sha == failed_assets_sha
    assert lineage.to_base_sha == failed_assets_sha

    wave_two = asyncio.run(
        host.dispatch_ready(
            host_task_id=task.task_id,
            state=state,
            max_parallel=4,
        )
    )
    assert {item.component_id for item in wave_two} == {"assets", "sdk"}
    assert all(
        item.disposition is ProgramWorkDisposition.REVIEW_REQUIRED
        for item in wave_two
    )
    _review(host, state, task_id=task.task_id, component_id="assets")
    _review(host, state, task_id=task.task_id, component_id="sdk")
    assert _record(state, "desktop").state is WorkState.READY

    final_wave = asyncio.run(
        host.dispatch_ready(
            host_task_id=task.task_id,
            state=state,
            max_parallel=2,
        )
    )
    assert [item.component_id for item in final_wave] == ["desktop"]
    assert final_wave[0].disposition is ProgramWorkDisposition.REVIEW_REQUIRED
    _review(host, state, task_id=task.task_id, component_id="desktop")
    final_snapshot = state.coordinator.snapshot()
    assert all(record.state is WorkState.ACCEPTED for record in final_snapshot.records)

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted_project = ProductProjectRepository(restarted_store).get(project.project_id)
    restarted_host = _host(
        restarted_store,
        worker,
        team_plan=team_plan,
        reviewer_principals=reviewer_principals,
    )
    restarted = restarted_host.restore(
        host_task_id=task.task_id,
        project=restarted_project,
    )

    assert restarted.authority.graph_digest == state.authority.graph_digest
    assert restarted.authority.graph_version == GRAPH_VERSION
    assert restarted.coordinator.snapshot() == final_snapshot
    assert restarted.binding.has_trusted_review_authority is True
