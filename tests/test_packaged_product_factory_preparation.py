from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_coordinator import ReviewDecision, WorkerResultEnvelope
from nika_core.product_factory_multi_repository import (
    MultiRepositoryExecutionError,
    MultiRepositoryProductFactoryHost,
    RepositoryGraphIntegrityError,
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
from nika_core.product_factory_review_authority import (
    reviewer_principal_bindings_ref,
    team_plan_fingerprint_ref,
)
from nika_core.product_factory_packaged_preparation import (
    PRODUCT_FACTORY_HOST_AGENT_ID,
    PackagedProductFactoryExecutionPlan,
    PackagedProductFactoryPreparationError,
    PackagedProductFactoryPreparationService,
    product_factory_host_task_identity,
)
from nika_core.product_project import ProductProjectRepository, ProductProjectSpec
from nika_core.toolsmith.contracts import (
    CodingResult,
    RecoveryState,
    TestEvidence,
    WorkerFailure,
    WorkerFailureKind,
)


class AllowReviewAuthority:
    def verify(self, subject, evidence_refs: tuple[str, ...]) -> bool:
        return bool(subject.project_id and evidence_refs)


class NeverDispatchWorker:
    def __init__(self) -> None:
        self.dispatch_calls = 0
        self.inspect_calls = 0
        self.recover_calls = 0

    async def dispatch(self, request):
        self.dispatch_calls += 1
        raise AssertionError(f"unexpected dispatch: {request.work_id}")

    async def inspect(self, work_id: str) -> RecoveryState | None:
        self.inspect_calls += 1
        raise AssertionError(f"unexpected inspect: {work_id}")

    async def recover(self, request, state):
        self.recover_calls += 1
        raise AssertionError(f"unexpected recover: {request.work_id}:{state}")


class VersionAdvancingResultWorker:
    def __init__(
        self,
        repository: ProductProjectRepository,
        project_id: str,
        *,
        advance_on: str,
    ) -> None:
        self.repository = repository
        self.project_id = project_id
        self.advance_on = advance_on
        self.dispatch_calls = 0
        self.inspect_calls = 0
        self.recover_calls = 0

    def _advance_project(self) -> None:
        latest = self.repository.get(self.project_id)
        self.repository.update_spec(
            latest.project_id,
            replace(
                latest.spec,
                desired_outcome=(
                    f"Concurrent ProductProject revision during worker {self.advance_on}"
                ),
            ),
            expected_row_version=latest.row_version,
            change_reason=f"regression: revise during worker {self.advance_on}",
        )

    @staticmethod
    def _result(request) -> WorkerResultEnvelope:
        return WorkerResultEnvelope(
            work_id=request.work_id,
            component_id=request.component_id,
            repository_id=request.repository_id,
            base_sha=request.base_sha,
            result_sha="b" * 40,
            diff_digest="d" * 64,
            coding_result=CodingResult(
                job_id=request.work_id,
                test_evidence=tuple(
                    TestEvidence(
                        command=command,
                        exit_code=0,
                        output_digest="e" * 64,
                    )
                    for command in request.acceptance_commands
                ),
            ),
            producer_actor_id="worker-actor",
        )

    async def dispatch(self, request):
        self.dispatch_calls += 1
        if self.advance_on == "dispatch":
            self._advance_project()
        return self._result(request)

    async def inspect(self, work_id: str) -> RecoveryState | None:
        self.inspect_calls += 1
        return RecoveryState(phase="running", opaque_token=f"resume:{work_id}")

    async def recover(self, request, state):
        self.recover_calls += 1
        if self.advance_on == "recover":
            self._advance_project()
        return self._result(request)


class FailureResultWorker:
    def __init__(self) -> None:
        self.dispatch_calls = 0

    async def dispatch(self, request):
        self.dispatch_calls += 1
        return WorkerResultEnvelope(
            work_id=request.work_id,
            component_id=request.component_id,
            repository_id=request.repository_id,
            base_sha=request.base_sha,
            result_sha="f" * 40,
            diff_digest="a" * 64,
            coding_result=CodingResult(
                job_id=request.work_id,
                failure=WorkerFailure(
                    kind=WorkerFailureKind.PROCESS_FAILED,
                    message="deterministic worker failure",
                    retryable=True,
                ),
            ),
            producer_actor_id="worker-actor",
        )

    async def inspect(self, work_id: str) -> RecoveryState | None:
        raise AssertionError(f"unexpected inspect: {work_id}")

    async def recover(self, request, state):
        raise AssertionError(f"unexpected recover: {request.work_id}:{state}")


def _fixture(tmp_path: Path):
    store = SQLiteStore(tmp_path / "product factory підготовка.db")
    store.initialize()
    repository = ProductProjectRepository(store)
    locator = "Oleksii-debug/Nika-Core"
    project = repository.create(
        project_id="product-preparation",
        name="Packaged Product Factory preparation",
        spec=ProductProjectSpec(
            goal="Prepare one trusted repository component",
            desired_outcome="Durable Product Factory authority exists",
            repository_refs=(locator,),
        ),
        idempotency_key="create:product-preparation",
    )
    graph = ProductRepositoryGraph(
        project_id=project.project_id,
        repositories=(
            RepositoryRef(
                repository_id="repo-core",
                provider="github",
                locator=locator,
                default_branch="main",
            ),
        ),
        components=(
            ProductComponent(
                component_id="core",
                repository_id="repo-core",
                paths=("src/nika_core",),
                test_commands=(("python", "-m", "pytest", "tests"),),
            ),
        ),
    )
    base_shas = {"repo-core": "a" * 40}
    component_goals = {"core": "Implement the exact accepted ProductProject work"}
    plan = PackagedProductFactoryExecutionPlan(
        project_id=project.project_id,
        expected_spec_version=project.spec_version,
        expected_row_version=project.row_version,
        graph=graph,
        graph_version=1,
        base_shas=base_shas,
        component_goals=component_goals,
        permission_ceiling=frozenset({"read_source", "write_source", "run_tests"}),
    )
    tasks = TaskQueue(store)
    host = MultiRepositoryProductFactoryHost(store, NeverDispatchWorker())
    service = PackagedProductFactoryPreparationService(
        repository=repository,
        tasks=tasks,
        host=host,
        workspace_id="packaged.product-factory",
    )
    return store, repository, tasks, service, project, graph, plan, base_shas, component_goals


def _task_count(store: SQLiteStore) -> int:
    with store.connection() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])


def _checkpoint_stage_count(
    store: SQLiteStore,
    *,
    task_id: str,
    stage: str,
) -> int:
    with store.connection() as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM checkpoints WHERE task_id = ? AND stage = ?",
                (task_id, stage),
            ).fetchone()[0]
        )


def test_prepare_is_exact_idempotent_and_restart_restores_same_authority(
    tmp_path: Path,
) -> None:
    store, repository, _tasks, service, project, _graph, plan, _bases, _goals = _fixture(
        tmp_path
    )

    first = service.prepare(plan)
    second = service.prepare(plan)

    assert first.host_task_id == second.host_task_id
    assert first.graph_digest == second.graph_digest
    assert first.state.coordinator.snapshot() == second.state.coordinator.snapshot()
    assert _task_count(store) == 1

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted = PackagedProductFactoryPreparationService(
        repository=ProductProjectRepository(restarted_store),
        tasks=TaskQueue(restarted_store),
        host=MultiRepositoryProductFactoryHost(restarted_store, NeverDispatchWorker()),
        workspace_id="packaged.product-factory",
    ).restore(project.project_id)

    assert restarted.host_task_id == first.host_task_id
    assert restarted.graph_digest == first.graph_digest
    assert restarted.state.coordinator.snapshot() == first.state.coordinator.snapshot()
    assert repository.get(project.project_id).row_version == project.row_version


def test_prepare_recovers_after_exact_host_task_exists_without_graph_checkpoint(
    tmp_path: Path,
) -> None:
    store, _repository, tasks, service, project, _graph, plan, _bases, _goals = _fixture(
        tmp_path
    )
    task_id = product_factory_host_task_identity(
        project.project_id,
        spec_version=project.spec_version,
        row_version=project.row_version,
    )
    tasks.create_exact(
        task_id=task_id,
        workspace_id="packaged.product-factory",
        agent_id=PRODUCT_FACTORY_HOST_AGENT_ID,
        payload={
            "kind": "product_factory",
            "product_project_id": project.project_id,
        },
    )

    prepared = service.prepare(plan)

    assert prepared.host_task_id == task_id
    assert prepared.state.authority.project_id == project.project_id
    assert _task_count(store) == 1


def test_same_project_version_rejects_competing_repository_graph(
    tmp_path: Path,
) -> None:
    store, _repository, _tasks, service, project, _graph, plan, _bases, _goals = _fixture(
        tmp_path
    )
    service.prepare(plan)
    competing_graph = ProductRepositoryGraph(
        project_id=project.project_id,
        repositories=plan.graph.repositories,
        components=(
            ProductComponent(
                component_id="core",
                repository_id="repo-core",
                paths=("src/other",),
                test_commands=(("python", "-m", "pytest", "tests"),),
            ),
        ),
    )
    competing = PackagedProductFactoryExecutionPlan(
        project_id=project.project_id,
        expected_spec_version=project.spec_version,
        expected_row_version=project.row_version,
        graph=competing_graph,
        graph_version=1,
        base_shas={"repo-core": "a" * 40},
        component_goals={"core": "Implement the exact accepted ProductProject work"},
        permission_ceiling=frozenset({"read_source", "write_source", "run_tests"}),
    )

    with pytest.raises(
        RepositoryGraphIntegrityError,
        match="authority does not match candidate graph",
    ):
        service.prepare(competing)

    assert _task_count(store) == 1


def test_stale_trusted_plan_fails_before_host_task_creation(tmp_path: Path) -> None:
    store, repository, _tasks, service, project, _graph, plan, _bases, _goals = _fixture(
        tmp_path
    )
    repository.update_spec(
        project.project_id,
        replace(
            project.spec,
            desired_outcome="A newer ProductProject outcome",
        ),
        expected_row_version=project.row_version,
        change_reason="test stale packaged preparation plan",
    )

    with pytest.raises(
        PackagedProductFactoryPreparationError,
        match="stale",
    ):
        service.prepare(plan)

    assert _task_count(store) == 0


def test_concurrent_project_revision_after_host_task_creation_blocks_graph_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, repository, _tasks, service, project, _graph, plan, _bases, _goals = _fixture(
        tmp_path
    )
    original_ensure = service._ensure_host_task

    def ensure_then_revise(current_project, *, task_id: str, create: bool) -> None:
        original_ensure(current_project, task_id=task_id, create=create)
        latest = repository.get(current_project.project_id)
        repository.update_spec(
            latest.project_id,
            replace(
                latest.spec,
                desired_outcome="Concurrent revision before graph authority publication",
            ),
            expected_row_version=latest.row_version,
            change_reason="regression: revise after exact host task creation",
        )

    monkeypatch.setattr(service, "_ensure_host_task", ensure_then_revise)

    task_id = product_factory_host_task_identity(
        project.project_id,
        spec_version=project.spec_version,
        row_version=project.row_version,
    )
    with pytest.raises(
        MultiRepositoryExecutionError,
        match="ProductProject changed before durable Product Factory authority publication",
    ):
        service.prepare(plan)

    assert _task_count(store) == 1
    assert (
        _checkpoint_stage_count(
            store,
            task_id=task_id,
            stage="product_factory.repository_graph.v1",
        )
        == 0
    )
    assert (
        _checkpoint_stage_count(
            store,
            task_id=task_id,
            stage="product_factory.coordinator.v1",
        )
        == 0
    )


def test_concurrent_project_revision_after_graph_binding_blocks_initial_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, repository, _tasks, service, project, _graph, plan, _bases, _goals = _fixture(
        tmp_path
    )
    original_bind_graph = service._host._bind_graph

    def bind_then_revise(
        *,
        host_task_id: str,
        project,
        graph,
        graph_version: int,
    ):
        authority = original_bind_graph(
            host_task_id=host_task_id,
            project=project,
            graph=graph,
            graph_version=graph_version,
        )
        latest = repository.get(project.project_id)
        repository.update_spec(
            latest.project_id,
            replace(
                latest.spec,
                desired_outcome="Concurrent revision before initial coordinator checkpoint",
            ),
            expected_row_version=latest.row_version,
            change_reason="regression: revise after graph authority publication",
        )
        return authority

    monkeypatch.setattr(service._host, "_bind_graph", bind_then_revise)

    task_id = product_factory_host_task_identity(
        project.project_id,
        spec_version=project.spec_version,
        row_version=project.row_version,
    )
    with pytest.raises(
        MultiRepositoryExecutionError,
        match="ProductProject changed before durable Product Factory authority publication",
    ):
        service.prepare(plan)

    assert (
        _checkpoint_stage_count(
            store,
            task_id=task_id,
            stage="product_factory.repository_graph.v1",
        )
        == 1
    )
    assert (
        _checkpoint_stage_count(
            store,
            task_id=task_id,
            stage="product_factory.coordinator.v1",
        )
        == 0
    )


def _revise_product_after_running_checkpoint(
    *,
    repository: ProductProjectRepository,
    project_id: str,
    service: PackagedProductFactoryPreparationService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    program_type = type(service._host._program)
    original_reserve = program_type._reserve_effect
    revised = False

    def reserve_after_revision(program, **kwargs):
        nonlocal revised
        if not revised:
            latest = repository.get(project_id)
            repository.update_spec(
                latest.project_id,
                replace(
                    latest.spec,
                    desired_outcome=(
                        "Concurrent ProductProject revision before worker effect admission"
                    ),
                ),
                expected_row_version=latest.row_version,
                change_reason="regression: revise before worker effect reservation",
            )
            revised = True
        return original_reserve(program, **kwargs)

    monkeypatch.setattr(
        program_type,
        "_reserve_effect",
        reserve_after_revision,
    )


def test_project_revision_after_running_checkpoint_blocks_worker_dispatch_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        _store,
        repository,
        _tasks,
        service,
        project,
        _graph,
        plan,
        _bases,
        _goals,
    ) = _fixture(tmp_path)
    prepared = service.prepare(plan)
    worker = service._host.worker
    assert isinstance(worker, NeverDispatchWorker)
    _revise_product_after_running_checkpoint(
        repository=repository,
        project_id=project.project_id,
        service=service,
        monkeypatch=monkeypatch,
    )

    with pytest.raises(
        MultiRepositoryExecutionError,
        match="ProductProject changed before durable Product Factory authority publication",
    ):
        asyncio.run(
            service._host.dispatch_ready(
                host_task_id=prepared.host_task_id,
                state=prepared.state,
                max_parallel=1,
                max_count=1,
            )
        )

    record = prepared.state.coordinator.snapshot().records[0]
    assert record.state.value == "running"
    assert worker.dispatch_calls == 0
    assert worker.inspect_calls == 0
    assert worker.recover_calls == 0


def test_stale_running_project_version_blocks_recovery_effect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        _store,
        repository,
        _tasks,
        service,
        project,
        _graph,
        plan,
        _bases,
        _goals,
    ) = _fixture(tmp_path)
    prepared = service.prepare(plan)
    worker = service._host.worker
    assert isinstance(worker, NeverDispatchWorker)
    _revise_product_after_running_checkpoint(
        repository=repository,
        project_id=project.project_id,
        service=service,
        monkeypatch=monkeypatch,
    )

    with pytest.raises(MultiRepositoryExecutionError):
        asyncio.run(
            service._host.dispatch_ready(
                host_task_id=prepared.host_task_id,
                state=prepared.state,
                max_parallel=1,
                max_count=1,
            )
        )
    assert prepared.state.coordinator.snapshot().records[0].state.value == "running"

    with pytest.raises(
        MultiRepositoryExecutionError,
        match="ProductProject changed before durable Product Factory authority publication",
    ):
        asyncio.run(
            service._host.recover_running(
                host_task_id=prepared.host_task_id,
                state=prepared.state,
                max_parallel=1,
            )
        )

    assert worker.dispatch_calls == 0
    assert worker.inspect_calls == 0
    assert worker.recover_calls == 0


@pytest.mark.parametrize("operation", ("dispatch_ready", "recover_running"))
def test_effect_admission_uses_pre_wait_project_version_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    (
        store,
        repository,
        _tasks,
        service,
        project,
        _graph,
        plan,
        _bases,
        _goals,
    ) = _fixture(tmp_path)
    prepared = service.prepare(plan)
    program_type = type(service._host._program)

    async def tamper_after_outer_assert(program, **kwargs):
        latest = repository.get(project.project_id)
        current = repository.update_spec(
            latest.project_id,
            replace(
                latest.spec,
                desired_outcome="Concurrent revision during async effect admission",
            ),
            expected_row_version=latest.row_version,
            change_reason="regression: mutate bound carrier after outer state assertion",
        )
        object.__setattr__(
            prepared.state.binding.project,
            "spec_version",
            current.spec_version,
        )
        object.__setattr__(
            prepared.state.binding.project,
            "row_version",
            current.row_version,
        )
        object.__setattr__(prepared.state.binding.project, "spec", current.spec)
        precondition = kwargs["effect_admission_precondition"]
        with store.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            precondition(connection)
        return ()

    monkeypatch.setattr(program_type, operation, tamper_after_outer_assert)
    call = getattr(service._host, operation)
    kwargs = {
        "host_task_id": prepared.host_task_id,
        "state": prepared.state,
        "max_parallel": 1,
    }
    if operation == "dispatch_ready":
        kwargs["max_count"] = 1

    with pytest.raises(
        MultiRepositoryExecutionError,
        match="ProductProject changed before durable Product Factory authority publication",
    ):
        asyncio.run(call(**kwargs))


def test_project_revision_during_worker_dispatch_blocks_stale_result_publication(
    tmp_path: Path,
) -> None:
    (
        _store,
        repository,
        _tasks,
        service,
        project,
        _graph,
        plan,
        _bases,
        _goals,
    ) = _fixture(tmp_path)
    prepared = service.prepare(plan)
    worker = VersionAdvancingResultWorker(
        repository,
        project.project_id,
        advance_on="dispatch",
    )
    service._host.worker = worker
    service._host._program.worker = worker

    outcomes = asyncio.run(
        service._host.dispatch_ready(
            host_task_id=prepared.host_task_id,
            state=prepared.state,
            max_parallel=1,
            max_count=1,
        )
    )

    assert len(outcomes) == 1
    assert outcomes[0].disposition.value == "uncertain"
    record = prepared.state.coordinator.snapshot().records[0]
    assert record.state.value == "running"
    assert record.result is None
    assert worker.dispatch_calls == 1
    assert repository.get(project.project_id).row_version > project.row_version


def test_project_revision_during_worker_recovery_blocks_stale_result_publication(
    tmp_path: Path,
) -> None:
    (
        _store,
        repository,
        _tasks,
        service,
        project,
        _graph,
        plan,
        _bases,
        _goals,
    ) = _fixture(tmp_path)
    prepared = service.prepare(plan)

    initial = asyncio.run(
        service._host.dispatch_ready(
            host_task_id=prepared.host_task_id,
            state=prepared.state,
            max_parallel=1,
            max_count=1,
        )
    )
    assert len(initial) == 1
    assert initial[0].disposition.value == "uncertain"
    assert prepared.state.coordinator.snapshot().records[0].state.value == "running"

    worker = VersionAdvancingResultWorker(
        repository,
        project.project_id,
        advance_on="recover",
    )
    service._host.worker = worker
    service._host._program.worker = worker

    outcomes = asyncio.run(
        service._host.recover_running(
            host_task_id=prepared.host_task_id,
            state=prepared.state,
            max_parallel=1,
        )
    )

    assert len(outcomes) == 1
    assert outcomes[0].disposition.value == "uncertain"
    record = prepared.state.coordinator.snapshot().records[0]
    assert record.state.value == "running"
    assert record.result is None
    assert worker.inspect_calls == 1
    assert worker.recover_calls == 1
    assert repository.get(project.project_id).row_version > project.row_version


def test_repair_lineage_uses_pre_tamper_project_version_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        store,
        repository,
        _tasks,
        service,
        project,
        _graph,
        plan,
        _bases,
        _goals,
    ) = _fixture(tmp_path)
    prepared = service.prepare(plan)
    worker = FailureResultWorker()
    service._host.worker = worker
    service._host._program.worker = worker

    outcomes = asyncio.run(
        service._host.dispatch_ready(
            host_task_id=prepared.host_task_id,
            state=prepared.state,
            max_parallel=1,
            max_count=1,
        )
    )
    assert len(outcomes) == 1
    assert outcomes[0].disposition.value == "repair_required"
    assert prepared.state.coordinator.snapshot().records[0].state.value == "repair_required"

    host_type = type(service._host)
    original_lineage_intent = host_type._lineage_intent

    def lineage_then_tamper(host, **kwargs):
        intent = original_lineage_intent(host, **kwargs)
        latest = repository.get(project.project_id)
        current = repository.update_spec(
            latest.project_id,
            replace(
                latest.spec,
                desired_outcome="Concurrent revision after repair preview",
            ),
            expected_row_version=latest.row_version,
            change_reason="regression: mutate bound project after repair preview",
        )
        object.__setattr__(
            prepared.state.binding.project,
            "spec_version",
            current.spec_version,
        )
        object.__setattr__(
            prepared.state.binding.project,
            "row_version",
            current.row_version,
        )
        object.__setattr__(prepared.state.binding.project, "spec", current.spec)
        return intent

    monkeypatch.setattr(host_type, "_lineage_intent", lineage_then_tamper)

    with pytest.raises(
        MultiRepositoryExecutionError,
        match="ProductProject changed before durable Product Factory authority publication",
    ):
        service._host.prepare_repair_and_checkpoint(
            host_task_id=prepared.host_task_id,
            state=prepared.state,
            component_id="core",
            reason="repair deterministic failure",
        )

    assert (
        _checkpoint_stage_count(
            store,
            task_id=prepared.host_task_id,
            stage="product_factory.repair_lineage.v1",
        )
        == 0
    )


def test_direct_restore_rejects_stale_product_project_version(
    tmp_path: Path,
) -> None:
    (
        _store,
        repository,
        _tasks,
        service,
        project,
        _graph,
        plan,
        _bases,
        _goals,
    ) = _fixture(tmp_path)
    prepared = service.prepare(plan)
    latest = repository.get(project.project_id)
    repository.update_spec(
        latest.project_id,
        replace(
            latest.spec,
            desired_outcome="Concurrent revision before direct Factory restore",
        ),
        expected_row_version=latest.row_version,
        change_reason="regression: revise before direct Factory restore",
    )

    with pytest.raises(
        MultiRepositoryExecutionError,
        match="ProductProject changed before durable Product Factory authority publication",
    ):
        service._host.restore(
            host_task_id=prepared.host_task_id,
            project=project,
        )


def test_initial_checkpoint_uses_bound_graph_project_version_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        store,
        repository,
        _tasks,
        service,
        project,
        _graph,
        plan,
        _bases,
        _goals,
    ) = _fixture(tmp_path)
    host_type = type(service._host)
    original_bind_graph = host_type._bind_graph

    def bind_then_revise(host, **kwargs):
        authority = original_bind_graph(host, **kwargs)
        project_carrier = kwargs["project"]
        latest = repository.get(project.project_id)
        current = repository.update_spec(
            latest.project_id,
            replace(
                latest.spec,
                desired_outcome="Concurrent revision after graph authority publication",
            ),
            expected_row_version=latest.row_version,
            change_reason="regression: mutate project carrier after graph binding",
        )
        object.__setattr__(project_carrier, "spec_version", current.spec_version)
        object.__setattr__(project_carrier, "row_version", current.row_version)
        object.__setattr__(project_carrier, "spec", current.spec)
        return authority

    monkeypatch.setattr(host_type, "_bind_graph", bind_then_revise)

    with pytest.raises(
        MultiRepositoryExecutionError,
        match="ProductProject changed before durable Product Factory authority publication",
    ):
        service.prepare(plan)

    with store.connection() as connection:
        stages = tuple(
            row["stage"]
            for row in connection.execute(
                "SELECT stage FROM checkpoints ORDER BY created_at, checkpoint_id"
            ).fetchall()
        )
    assert stages == ("product_factory.repository_graph.v1",)


def test_execution_plan_snapshots_mutable_graph_and_mapping_inputs(tmp_path: Path) -> None:
    (
        _store,
        _repository,
        _tasks,
        service,
        project,
        graph,
        plan,
        base_shas,
        component_goals,
    ) = _fixture(tmp_path)
    base_shas["repo-core"] = "b" * 40
    component_goals["core"] = "mutated caller goal"
    graph.components = (
        ProductComponent(
            component_id="core",
            repository_id="repo-core",
            paths=("src/mutated",),
            test_commands=(("python", "-m", "pytest", "tests"),),
        ),
    )

    prepared = service.prepare(plan)

    request = prepared.state.coordinator.snapshot().records[0].request
    assert request.base_sha == "a" * 40
    assert request.goal == "Implement the exact accepted ProductProject work"
    assert request.allowed_paths == ("src/nika_core",)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("expected_spec_version", True, "positive integer"),
        ("expected_row_version", False, "non-negative integer"),
        ("base_shas", ["repo-core", "a" * 40], "must be a mapping"),
        ("permission_ceiling", {"read_source"}, "non-empty frozenset"),
    ),
)
def test_prepare_revalidates_tampered_frozen_execution_plan_before_effect(
    tmp_path: Path,
    field: str,
    value: object,
    message: str,
) -> None:
    store, _repository, _tasks, service, _project, _graph, plan, _bases, _goals = (
        _fixture(tmp_path)
    )
    object.__setattr__(plan, field, value)

    with pytest.raises(PackagedProductFactoryPreparationError, match=message):
        service.prepare(plan)

    assert _task_count(store) == 0


def test_prepare_normalizes_hostile_mapping_snapshot_failure_before_effect(
    tmp_path: Path,
) -> None:
    class ExplodingMapping(dict):
        def items(self):
            raise RuntimeError("untrusted mapping iterator failure")

    store, _repository, _tasks, service, _project, _graph, plan, _bases, _goals = (
        _fixture(tmp_path)
    )
    object.__setattr__(
        plan,
        "base_shas",
        ExplodingMapping({"repo-core": "a" * 40}),
    )

    with pytest.raises(
        PackagedProductFactoryPreparationError,
        match="base_shas could not be snapshotted safely",
    ):
        service.prepare(plan)

    assert _task_count(store) == 0


def test_prepare_rejects_structurally_deleted_execution_plan_before_effect(
    tmp_path: Path,
) -> None:
    store, _repository, _tasks, service, _project, _graph, plan, _bases, _goals = (
        _fixture(tmp_path)
    )
    object.__delattr__(plan, "graph")

    with pytest.raises(
        PackagedProductFactoryPreparationError,
        match="execution plan is structurally invalid",
    ):
        service.prepare(plan)

    assert _task_count(store) == 0


@pytest.mark.parametrize(
    ("target", "field", "value", "message"),
    (
        ("repository", "case_sensitive_paths", "false", "case_sensitive_paths"),
        ("component", "paths", ["src/nika_core"], "component paths"),
        (
            "component",
            "build_commands",
            (["python", "-m", "pytest"],),
            "component build_commands",
        ),
        ("component", "release_identity", 7, "release_identity"),
    ),
)
def test_prepare_revalidates_tampered_nested_graph_carriers_before_effect(
    tmp_path: Path,
    target: str,
    field: str,
    value: object,
    message: str,
) -> None:
    store, _repository, _tasks, service, _project, _graph, plan, _bases, _goals = (
        _fixture(tmp_path)
    )
    carrier = (
        plan.graph.repositories[0]
        if target == "repository"
        else plan.graph.components[0]
    )
    object.__setattr__(carrier, field, value)

    with pytest.raises(PackagedProductFactoryPreparationError, match=message):
        service.prepare(plan)

    assert _task_count(store) == 0


def test_prepare_normalizes_tampered_graph_invariant_failure_before_effect(
    tmp_path: Path,
) -> None:
    store, _repository, _tasks, service, _project, _graph, plan, _bases, _goals = (
        _fixture(tmp_path)
    )
    object.__setattr__(
        plan.graph.components[0],
        "repository_id",
        "missing-repository",
    )

    with pytest.raises(
        PackagedProductFactoryPreparationError,
        match="repository graph is structurally invalid",
    ):
        service.prepare(plan)

    assert _task_count(store) == 0


def test_prepare_rejects_structurally_deleted_nested_graph_field_before_effect(
    tmp_path: Path,
) -> None:
    store, _repository, _tasks, service, _project, _graph, plan, _bases, _goals = (
        _fixture(tmp_path)
    )
    object.__delattr__(plan.graph.repositories[0], "locator")

    with pytest.raises(
        PackagedProductFactoryPreparationError,
        match="repository entry is structurally invalid",
    ):
        service.prepare(plan)

    assert _task_count(store) == 0


def test_prepare_uses_detached_snapshot_if_original_plan_mutates_mid_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (
        _store,
        repository,
        _tasks,
        service,
        _project,
        _graph,
        plan,
        _bases,
        _goals,
    ) = _fixture(tmp_path)
    original_get = ProductProjectRepository.get

    def get_then_mutate(self, project_id: str):
        current = original_get(self, project_id)
        object.__setattr__(plan, "base_shas", {"repo-core": "b" * 40})
        object.__setattr__(plan, "component_goals", {"core": "mutated caller goal"})
        object.__setattr__(plan, "permission_ceiling", frozenset({"read_source"}))
        return current

    monkeypatch.setattr(ProductProjectRepository, "get", get_then_mutate)

    prepared = service.prepare(plan)

    request = prepared.state.coordinator.snapshot().records[0].request
    assert request.base_sha == "a" * 40
    assert request.goal == "Implement the exact accepted ProductProject work"
    assert request.permission_ceiling == frozenset(
        {"read_source", "write_source", "run_tests"}
    )


def test_deterministic_host_task_collision_fails_closed(tmp_path: Path) -> None:
    (
        store,
        _repository,
        tasks,
        service,
        project,
        _graph,
        plan,
        _bases,
        _goals,
    ) = _fixture(tmp_path)
    task_id = product_factory_host_task_identity(
        project.project_id,
        spec_version=project.spec_version,
        row_version=project.row_version,
    )
    tasks.create_exact(
        task_id=task_id,
        workspace_id="other-workspace",
        agent_id=PRODUCT_FACTORY_HOST_AGENT_ID,
        payload={
            "kind": "product_factory",
            "product_project_id": project.project_id,
        },
    )

    with pytest.raises(
        PackagedProductFactoryPreparationError,
        match="conflicts with existing authority",
    ):
        service.prepare(plan)

    assert _task_count(store) == 1


def test_require_repair_request_never_upgrades_nonfailed_work(tmp_path: Path) -> None:
    _store, _repository, _tasks, service, project, _graph, plan, _bases, _goals = _fixture(
        tmp_path
    )
    prepared = service.prepare(plan)
    record = prepared.state.coordinator.snapshot().records[0]
    assert record.state.value == "ready"

    with pytest.raises(
        PackagedProductFactoryPreparationError,
        match="not an exact durable REPAIR_REQUIRED",
    ):
        service.require_repair_request(project.project_id, "core")


def test_execution_plan_rejects_digest_length_as_repository_base_sha(
    tmp_path: Path,
) -> None:
    (
        _store,
        _repository,
        _tasks,
        _service,
        project,
        graph,
        _plan,
        _bases,
        _goals,
    ) = _fixture(tmp_path)

    with pytest.raises(
        PackagedProductFactoryPreparationError,
        match="base SHA is invalid",
    ):
        PackagedProductFactoryExecutionPlan(
            project_id=project.project_id,
            expected_spec_version=project.spec_version,
            expected_row_version=project.row_version,
            graph=graph,
            graph_version=1,
            base_shas={"repo-core": "a" * 64},
            component_goals={"core": "Implement the exact accepted ProductProject work"},
            permission_ceiling=frozenset({"read_source", "write_source", "run_tests"}),
        )


def test_preparation_preserves_persisted_team_review_authority(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "trusted review preparation.db")
    store.initialize()
    repository = ProductProjectRepository(store)
    locator = "Oleksii-debug/Nika-Core"
    permissions = frozenset({"read_source", "write_source", "run_tests"})
    team_plan = DynamicTeamComposer().compose(
        TeamCompositionRequest(
            project_id="product-team-preparation",
            components=(ComponentBrief("core", "backend"),),
            acceptance_criteria=("Independent review is required",),
            permission_ceiling=permissions,
            scale=ProjectScale.SMALL,
        )
    )
    reviewer_role = next(role for role in team_plan.roles if role.independent_review)
    reviewer_principals = ((reviewer_role.role_id, "reviewer-actor"),)
    project = repository.create(
        project_id=team_plan.project_id,
        name="Trusted review Product Factory preparation",
        spec=ProductProjectSpec(
            goal="Prepare reviewed component work",
            desired_outcome="Trusted review authority survives Product Factory preparation",
            repository_refs=(locator,),
            team_refs=(
                team_plan.plan_id,
                team_plan_fingerprint_ref(team_plan),
                reviewer_principal_bindings_ref(team_plan, reviewer_principals),
            ),
        ),
        idempotency_key="create:product-team-preparation",
    )
    graph = ProductRepositoryGraph(
        project_id=project.project_id,
        repositories=(RepositoryRef("repo-core", "github", locator, "main"),),
        components=(
            ProductComponent(
                component_id="core",
                repository_id="repo-core",
                paths=("src/nika_core",),
                test_commands=(("python", "-m", "pytest", "tests"),),
            ),
        ),
    )
    evidence_authority = AllowReviewAuthority()
    host = MultiRepositoryProductFactoryHost(
        store,
        NeverDispatchWorker(),
        team_plan=team_plan,
        review_evidence_authority=evidence_authority,
        reviewer_principals=reviewer_principals,
    )
    service = PackagedProductFactoryPreparationService(
        repository=repository,
        tasks=TaskQueue(store),
        host=host,
        workspace_id="packaged.product-factory",
    )
    plan = PackagedProductFactoryExecutionPlan(
        project_id=project.project_id,
        expected_spec_version=project.spec_version,
        expected_row_version=project.row_version,
        graph=graph,
        graph_version=1,
        base_shas={"repo-core": "c" * 40},
        component_goals={"core": "Implement reviewed work"},
        permission_ceiling=permissions,
    )

    prepared = service.prepare(plan)
    restored = service.restore(project.project_id)

    assert prepared.state.binding.has_trusted_review_authority is True
    assert restored.state.binding.has_trusted_review_authority is True
    assert restored.state.binding.team_plan == team_plan
    assert restored.state.binding.reviewer_principals == reviewer_principals


def test_product_revision_blocks_stale_review_checkpoint(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "trusted stale review.db")
    store.initialize()
    repository = ProductProjectRepository(store)
    locator = "Oleksii-debug/Nika-Core"
    permissions = frozenset({"read_source", "write_source", "run_tests"})
    team_plan = DynamicTeamComposer().compose(
        TeamCompositionRequest(
            project_id="product-review-stale",
            components=(ComponentBrief("core", "backend"),),
            acceptance_criteria=("Independent review is required",),
            permission_ceiling=permissions,
            scale=ProjectScale.SMALL,
        )
    )
    reviewer_role = next(role for role in team_plan.roles if role.independent_review)
    reviewer_principals = ((reviewer_role.role_id, "reviewer-actor"),)
    project = repository.create(
        project_id=team_plan.project_id,
        name="Stale review Product Factory",
        spec=ProductProjectSpec(
            goal="Review one trusted component",
            desired_outcome="Review stays bound to the exact ProductProject version",
            repository_refs=(locator,),
            team_refs=(
                team_plan.plan_id,
                team_plan_fingerprint_ref(team_plan),
                reviewer_principal_bindings_ref(team_plan, reviewer_principals),
            ),
        ),
        idempotency_key="create:product-review-stale",
    )
    graph = ProductRepositoryGraph(
        project_id=project.project_id,
        repositories=(RepositoryRef("repo-core", "github", locator, "main"),),
        components=(
            ProductComponent(
                component_id="core",
                repository_id="repo-core",
                paths=("src/nika_core",),
                test_commands=(("python", "-m", "pytest", "tests"),),
            ),
        ),
    )
    evidence_authority = AllowReviewAuthority()
    host = MultiRepositoryProductFactoryHost(
        store,
        NeverDispatchWorker(),
        team_plan=team_plan,
        review_evidence_authority=evidence_authority,
        reviewer_principals=reviewer_principals,
    )
    service = PackagedProductFactoryPreparationService(
        repository=repository,
        tasks=TaskQueue(store),
        host=host,
        workspace_id="packaged.product-factory",
    )
    plan = PackagedProductFactoryExecutionPlan(
        project_id=project.project_id,
        expected_spec_version=project.spec_version,
        expected_row_version=project.row_version,
        graph=graph,
        graph_version=1,
        base_shas={"repo-core": "c" * 40},
        component_goals={"core": "Implement reviewed work"},
        permission_ceiling=permissions,
    )
    prepared = service.prepare(plan)
    worker = VersionAdvancingResultWorker(
        repository,
        project.project_id,
        advance_on="none",
    )
    service._host.worker = worker
    service._host._program.worker = worker

    outcomes = asyncio.run(
        service._host.dispatch_ready(
            host_task_id=prepared.host_task_id,
            state=prepared.state,
            max_parallel=1,
            max_count=1,
        )
    )
    assert len(outcomes) == 1
    assert outcomes[0].disposition.value == "review_required"

    latest = repository.get(project.project_id)
    repository.update_spec(
        latest.project_id,
        replace(
            latest.spec,
            desired_outcome="Concurrent revision before trusted review checkpoint",
        ),
        expected_row_version=latest.row_version,
        change_reason="regression: revise before review checkpoint",
    )

    with pytest.raises(
        MultiRepositoryExecutionError,
        match="ProductProject changed before durable Product Factory authority publication",
    ):
        service._host.review_and_checkpoint(
            host_task_id=prepared.host_task_id,
            state=prepared.state,
            component_id="core",
            decision=ReviewDecision(
                reviewer_id="reviewer-actor",
                accepted=True,
                reason="independent evidence accepted",
                evidence_refs=("review:evidence",),
            ),
        )

    record = prepared.state.coordinator.snapshot().records[0]
    assert record.state.value == "review_required"
    assert record.review is None

