from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_command.contracts import ProductStatusKind
from nika_core.product_factory_build_execution import (
    BuildExecutionRecord,
    BuildExecutionScopeRequest,
    BuildExecutionSnapshot,
    BuildExecutionSpec,
    BuildExecutionState,
    ExecutionGrant,
)
from nika_core.product_factory_build_execution_persistence import (
    DurableBuildExecutionSnapshot,
    SQLiteBuildExecutionCheckpointStore,
)
from nika_core.product_command.product_project_adapter import (
    ProductProjectCommandService,
    ProductProjectPresentationConsistencyError,
)
from nika_core.product_decisions import ProductDecisionRepository
from nika_core.product_factory_deployment import ExecutionRequest, Platform, ResourceEnvelope
from nika_core.product_factory_multi_repository import MultiRepositoryProductFactoryHost
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from nika_core.product_factory_packaged_journey import (
    PackagedProductCommandRouter,
    PackagedProductSelectionStore,
    PackagedProductStateProvider,
    packaged_current_product_factory_status_command,
)
from nika_core.product_factory_packaged_preparation import (
    PRODUCT_FACTORY_HOST_AGENT_ID,
    PackagedProductFactoryExecutionPlan,
    PackagedProductFactoryPreparationService,
    product_factory_host_task_identity,
)
from nika_core.product_factory_packaged_status import (
    PACKAGED_PRODUCT_FACTORY_WORKSPACE_ID,
    PackagedProductCommandCenter,
    PackagedProductFactoryStatusReader,
)
from nika_core.product_project import ProductProjectRepository, ProductProjectSpec
from nika_core.toolsmith.contracts import RecoveryState


class NeverDispatchWorker:
    async def dispatch(self, request):
        raise AssertionError(f"unexpected dispatch: {request.work_id}")

    async def inspect(self, work_id: str) -> RecoveryState | None:
        raise AssertionError(f"unexpected inspect: {work_id}")

    async def recover(self, request, state):
        raise AssertionError(f"unexpected recover: {request.work_id}:{state}")


def _fixture(tmp_path: Path):
    store = SQLiteStore(tmp_path / "packaged factory status.db")
    store.initialize()
    repository = ProductProjectRepository(store)
    project = repository.create(
        project_id="product-packaged-status",
        name="Packaged Product Factory status",
        spec=ProductProjectSpec(
            goal="Show durable component execution status",
            desired_outcome="Windows user can inspect trusted Product Factory state",
            repository_refs=("Oleksii-debug/Nika-Core",),
        ),
        idempotency_key="create:product-packaged-status",
    )
    graph = ProductRepositoryGraph(
        project_id=project.project_id,
        repositories=(
            RepositoryRef(
                repository_id="repo-core",
                provider="github",
                locator="Oleksii-debug/Nika-Core",
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
    plan = PackagedProductFactoryExecutionPlan(
        project_id=project.project_id,
        expected_spec_version=project.spec_version,
        expected_row_version=project.row_version,
        graph=graph,
        graph_version=1,
        base_shas={"repo-core": "a" * 40},
        component_goals={"core": "Implement the accepted ProductProject work"},
        permission_ceiling=frozenset({"read_source", "write_source", "run_tests"}),
    )
    preparation = PackagedProductFactoryPreparationService(
        repository=repository,
        tasks=TaskQueue(store),
        host=MultiRepositoryProductFactoryHost(store, NeverDispatchWorker()),
        workspace_id=PACKAGED_PRODUCT_FACTORY_WORKSPACE_ID,
    )
    products = ProductProjectCommandService(repository)
    center = PackagedProductCommandCenter(
        products=products,
        status_reader=PackagedProductFactoryStatusReader(store),
    )
    return store, repository, project, plan, preparation, center


_PF5_WORK_ID = "pf5-build:" + "1" * 64
_PF5_NOW = datetime(2026, 10, 6, 20, 0, tzinfo=UTC)


def _pf5_record(
    project_id: str,
    *,
    state: BuildExecutionState = BuildExecutionState.PENDING,
    updated_at: datetime = _PF5_NOW,
) -> BuildExecutionRecord:
    spec = BuildExecutionSpec(
        request=ExecutionRequest(
            project_id=project_id,
            work_id=_PF5_WORK_ID,
            platform=Platform.WINDOWS,
            required_features=frozenset({"build"}),
            required_toolchains=frozenset({"python"}),
            resources=ResourceEnvelope(2, 2048, 4096),
        ),
        source_sha="b" * 40,
        scope=BuildExecutionScopeRequest(
            repository_id="repo-core",
            workspace_relpath="src/nika_core",
            requested_node_ids=("windows-build-1",),
            command_id="build",
        ),
        lease_seconds=120,
    )
    grant = ExecutionGrant(
        project_id=project_id,
        repository_id="repo-core",
        work_id=_PF5_WORK_ID,
        workspace_relpath="src/nika_core",
        allowed_node_ids=("windows-build-1",),
        network_scopes=(),
        credential_refs=(),
        command_id="build",
        argv=("python", "-m", "build"),
        authority_evidence_refs=("authority://packaged-pf5/test",),
    )
    return BuildExecutionRecord(
        spec=spec,
        grant=grant,
        state=state,
        block_reason=(
            "trusted execution authority changed"
            if state is BuildExecutionState.WAITING_FOR_AUTHORITY
            else None
        ),
        updated_at=updated_at,
    )


def _save_pf5_status(
    store: SQLiteStore,
    *,
    host_task_id: str,
    project_id: str,
    state: BuildExecutionState,
) -> None:
    checkpoints = SQLiteBuildExecutionCheckpointStore(
        store,
        host_task_id,
        project_id,
    )
    pending = _pf5_record(project_id)
    checkpoints.save(
        DurableBuildExecutionSnapshot(
            sequence=1,
            coordinator=BuildExecutionSnapshot((pending,)),
            leases=(),
            registry_next_lease=1,
            file_evidence=(),
        )
    )
    if state is BuildExecutionState.PENDING:
        return
    current = replace(
        pending,
        state=state,
        block_reason=(
            "trusted execution authority changed"
            if state is BuildExecutionState.WAITING_FOR_AUTHORITY
            else None
        ),
        updated_at=_PF5_NOW + timedelta(seconds=1),
    )
    checkpoints.save(
        DurableBuildExecutionSnapshot(
            sequence=2,
            coordinator=BuildExecutionSnapshot((current,)),
            leases=(),
            registry_next_lease=1,
            file_evidence=(),
        )
    )


def test_unprepared_current_project_keeps_existing_pf5_projection(tmp_path: Path) -> None:
    _store, _repository, project, _plan, _preparation, center = _fixture(tmp_path)

    detail = center.inspect_project(project.project_id)

    assert detail.summary.project_id == project.project_id
    assert detail.statuses == ()
    assert detail.summary.blocker_count == 0


def test_prepared_component_uses_canonical_coordinator_status_projection(tmp_path: Path) -> None:
    _store, _repository, project, plan, preparation, center = _fixture(tmp_path)
    prepared = preparation.prepare(plan)

    detail = center.inspect_project(project.project_id)

    components = tuple(
        item for item in detail.statuses if item.kind is ProductStatusKind.COMPONENT
    )
    assert len(components) == 1
    assert components[0].item_id == "core"
    assert components[0].state == "ready"
    assert "Repository: repo-core" in components[0].detail
    assert "Base SHA: " + "a" * 40 in components[0].detail
    assert prepared.state.coordinator.snapshot().records[0].state.value == "ready"
    assert detail.summary.blocker_count == 0


def test_restart_reads_same_canonical_checkpoint_without_worker_runtime(tmp_path: Path) -> None:
    store, _repository, project, plan, preparation, center = _fixture(tmp_path)
    preparation.prepare(plan)
    before = center.inspect_project(project.project_id)

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted_center = PackagedProductCommandCenter(
        products=ProductProjectCommandService(ProductProjectRepository(restarted_store)),
        status_reader=PackagedProductFactoryStatusReader(restarted_store),
    )
    after = restarted_center.inspect_project(project.project_id)

    assert after.statuses == before.statuses
    assert after.summary.blocker_count == before.summary.blocker_count


def test_new_productproject_version_never_reuses_stale_factory_status(tmp_path: Path) -> None:
    _store, repository, project, plan, preparation, center = _fixture(tmp_path)
    preparation.prepare(plan)
    assert center.inspect_project(project.project_id).statuses

    repository.update_spec(
        project.project_id,
        replace(project.spec, desired_outcome="New accepted ProductProject version"),
        expected_row_version=project.row_version,
        change_reason="status projection stale-version regression",
    )

    current = center.inspect_project(project.project_id)
    assert current.summary.version == project.spec_version + 1
    assert current.statuses == ()




def test_status_change_during_pf5_composition_fails_closed(tmp_path: Path) -> None:
    _store, repository, project, plan, preparation, _center = _fixture(tmp_path)
    prepared = preparation.prepare(plan)
    stable = prepared.state.coordinator.snapshot()

    class ChangingStatusReader:
        def __init__(self) -> None:
            self.calls = 0

        def read(self, project_id: str):
            assert project_id == project.project_id
            self.calls += 1
            return stable if self.calls == 1 else None

        def read_build_execution(self, project_id: str):
            assert project_id == project.project_id
            return None

    reader = ChangingStatusReader()
    center = PackagedProductCommandCenter(
        products=ProductProjectCommandService(repository),
        status_reader=reader,  # type: ignore[arg-type]
    )

    with pytest.raises(
        ProductProjectPresentationConsistencyError,
        match="status changed while PF5",
    ):
        center.inspect_packaged_project(project.project_id)

    assert reader.calls == 2


def test_durable_pf5_build_status_survives_restart_and_reaches_command(
    tmp_path: Path,
) -> None:
    store, repository, project, plan, preparation, center = _fixture(tmp_path)
    prepared = preparation.prepare(plan)
    _save_pf5_status(
        store,
        host_task_id=prepared.host_task_id,
        project_id=project.project_id,
        state=BuildExecutionState.PENDING,
    )

    detail = center.inspect_project(project.project_id)
    builds = tuple(
        item for item in detail.statuses if item.item_id.startswith("pf5-build:")
    )
    assert len(builds) == 1
    assert builds[0].kind is ProductStatusKind.BUILD
    assert builds[0].state == "pending"
    assert builds[0].label == "PF5 build: repo-core"
    assert "credential" not in builds[0].detail.casefold()
    assert "authority://" not in builds[0].detail
    assert detail.summary.blocker_count == 0

    router = _status_router(
        store=store,
        repository=repository,
        project_id=project.project_id,
        center=center,
    )
    before = router.create({"command": "Show current Product Factory status"})
    assert "PF5 buildів 1" in before.message
    assert "стани PF5: pending=1" in before.message

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted_repository = ProductProjectRepository(restarted_store)
    restarted_center = PackagedProductCommandCenter(
        products=ProductProjectCommandService(restarted_repository),
        status_reader=PackagedProductFactoryStatusReader(restarted_store),
    )
    restarted_router = _status_router(
        store=restarted_store,
        repository=restarted_repository,
        project_id=project.project_id,
        center=restarted_center,
    )

    assert restarted_router.create({"command": "Show current Product Factory status"}) == before


def test_waiting_for_authority_pf5_build_is_visible_product_blocker(tmp_path: Path) -> None:
    store, repository, project, plan, preparation, center = _fixture(tmp_path)
    prepared = preparation.prepare(plan)
    _save_pf5_status(
        store,
        host_task_id=prepared.host_task_id,
        project_id=project.project_id,
        state=BuildExecutionState.WAITING_FOR_AUTHORITY,
    )

    detail = center.inspect_project(project.project_id)
    blocker = next(item for item in detail.statuses if item.item_id == _PF5_WORK_ID)
    assert blocker.kind is ProductStatusKind.BLOCKER
    assert blocker.state == "waiting_for_authority"
    assert detail.summary.blocker_count == 1

    result = _status_router(
        store=store,
        repository=repository,
        project_id=project.project_id,
        center=center,
    ).create({"command": "Покажи поточний статус Product Factory"})
    assert "блокерів 1" in result.message
    assert "стани PF5: waiting_for_authority=1" in result.message


def test_corrupt_pf5_checkpoint_never_becomes_product_status(tmp_path: Path) -> None:
    store, _repository, project, plan, preparation, center = _fixture(tmp_path)
    prepared = preparation.prepare(plan)
    _save_pf5_status(
        store,
        host_task_id=prepared.host_task_id,
        project_id=project.project_id,
        state=BuildExecutionState.PENDING,
    )
    with store.connection() as conn:
        conn.execute(
            "UPDATE checkpoints SET checksum_sha256 = ? WHERE task_id = ? "
            "AND stage = 'product_factory.build_execution.v1'",
            ("0" * 64, prepared.host_task_id),
        )

    with pytest.raises(
        ProductProjectPresentationConsistencyError,
        match="trusted projection",
    ):
        center.inspect_project(project.project_id)


def test_existing_host_without_checkpoint_fails_closed(tmp_path: Path) -> None:
    store, _repository, project, _plan, _preparation, center = _fixture(tmp_path)
    task_id = product_factory_host_task_identity(
        project.project_id,
        spec_version=project.spec_version,
        row_version=project.row_version,
    )
    TaskQueue(store).create_exact(
        task_id=task_id,
        workspace_id=PACKAGED_PRODUCT_FACTORY_WORKSPACE_ID,
        agent_id=PRODUCT_FACTORY_HOST_AGENT_ID,
        payload={"kind": "product_factory", "product_project_id": project.project_id},
    )

    with pytest.raises(
        ProductProjectPresentationConsistencyError,
        match="trusted projection",
    ):
        center.inspect_project(project.project_id)


def test_conflicting_deterministic_host_identity_fails_closed(tmp_path: Path) -> None:
    store, _repository, project, _plan, _preparation, center = _fixture(tmp_path)
    task_id = product_factory_host_task_identity(
        project.project_id,
        spec_version=project.spec_version,
        row_version=project.row_version,
    )
    TaskQueue(store).create_exact(
        task_id=task_id,
        workspace_id="foreign-workspace",
        agent_id=PRODUCT_FACTORY_HOST_AGENT_ID,
        payload={"kind": "product_factory", "product_project_id": project.project_id},
    )

    with pytest.raises(
        ProductProjectPresentationConsistencyError,
        match="trusted projection",
    ):
        center.inspect_project(project.project_id)


def test_corrupt_checkpoint_head_never_becomes_windows_status(tmp_path: Path) -> None:
    store, _repository, project, plan, preparation, center = _fixture(tmp_path)
    prepared = preparation.prepare(plan)
    with store.connection() as conn:
        conn.execute(
            "UPDATE checkpoints SET checksum_sha256 = ? WHERE task_id = ? "
            "AND stage = 'product_factory.coordinator.v1'",
            ("0" * 64, prepared.host_task_id),
        )

    with pytest.raises(
        ProductProjectPresentationConsistencyError,
        match="trusted projection",
    ):
        center.inspect_project(project.project_id)


def test_repeated_status_reads_are_read_only(tmp_path: Path) -> None:
    store, _repository, project, plan, preparation, center = _fixture(tmp_path)
    preparation.prepare(plan)

    def counts() -> tuple[int, int, int]:
        with store.connection() as conn:
            return tuple(
                int(conn.execute(statement).fetchone()[0])
                for statement in (
                    "SELECT COUNT(*) FROM tasks",
                    "SELECT COUNT(*) FROM checkpoints",
                    "SELECT COUNT(*) FROM audit_events",
                )
            )

    before = counts()
    for _ in range(5):
        detail = center.inspect_project(project.project_id)
        assert any(item.kind is ProductStatusKind.COMPONENT for item in detail.statuses)
    assert counts() == before




class SelectedProjectRouter:
    def __init__(self, project_id: str) -> None:
        self.active_project_id = project_id

    def clear_stale_selection(self) -> None:
        self.active_project_id = None


def test_packaged_state_exposes_bounded_component_status_without_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _store, _repository, project, plan, preparation, center = _fixture(tmp_path)
    preparation.prepare(plan)

    def fail_unbounded_list(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("Factory status projection must keep bounded decision reads")

    monkeypatch.setattr(ProductDecisionRepository, "list", fail_unbounded_list)
    router = SelectedProjectRouter(project.project_id)
    provider = PackagedProductStateProvider(
        base_state=lambda: {"tasks": [], "agents": [], "workspaces": []},
        router=router,  # type: ignore[arg-type]
        command_center=center,  # type: ignore[arg-type]
    )

    state = provider()
    product_state = state["product_project"]

    assert product_state["decision_count"] == 0
    assert product_state["decision_state_counts"] == {}
    assert product_state["status_count"] == 1
    assert product_state["status_counts"] == {"component": 1}
    assert product_state["status_items"] == [
        {
            "kind": "component",
            "item_id": "core",
            "label": "Компонент core",
            "state": "ready",
            "detail": (
                "Стан: Готово до виконання; Repository: repo-core; Base SHA: "
                + "a" * 40
                + "; Attempt: 1; Allowed paths: src/nika_core"
            ),
        }
    ]
    assert product_state["status_items_truncated"] is False
    assert "evidence" not in product_state["status_items"][0]
    assert product_state["operator"] == {
        "project": project.project_id,
        "work": "core=ready",
        "owner": "unassigned",
        "state": "active",
        "blocker": "none",
        "candidate": "unknown",
        "test": "unknown",
        "qa": "unknown",
        "integration": "not_started",
        "next": "continue_work:core",
    }


def _status_router(
    *,
    store: SQLiteStore,
    repository: ProductProjectRepository,
    project_id: str,
    center: PackagedProductCommandCenter,
) -> PackagedProductCommandRouter:
    selection = PackagedProductSelectionStore(store)
    selection.select(project_id)
    return PackagedProductCommandRouter(
        products=ProductProjectCommandService(repository),
        ordinary_handler=lambda _payload: pytest.fail(
            "Factory status command must not fall through to ordinary task routing"
        ),
        selection_store=selection,
        product_factory_status_inspector=center.inspect_packaged_project,
    )


def test_factory_status_command_is_exact_and_does_not_capture_broad_text() -> None:
    assert packaged_current_product_factory_status_command(
        "Show current Product Factory status"
    )
    assert packaged_current_product_factory_status_command(
        "Покажи поточний статус Product Factory."
    )
    assert packaged_current_product_factory_status_command(
        "Покажи поточний стан Product Factory."
    )
    assert packaged_current_product_factory_status_command(
        "Статус поточного Product Factory."
    )
    assert not packaged_current_product_factory_status_command(
        "please show current Product Factory status when convenient"
    )


def test_factory_status_command_reports_prepared_authority_and_survives_restart(
    tmp_path: Path,
) -> None:
    store, repository, project, plan, preparation, center = _fixture(tmp_path)
    preparation.prepare(plan)
    router = _status_router(
        store=store,
        repository=repository,
        project_id=project.project_id,
        center=center,
    )

    before = router.create({"command": "Покажи поточний статус Product Factory"})

    assert before.status == "completed"
    assert before.focus_id == "product-project-operator-heading"
    assert f"Статус Product Factory для {project.project_id}" in before.message
    assert "компонентів 1" in before.message
    assert "блокерів 0" in before.message
    assert "core=ready" in before.message

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted_repository = ProductProjectRepository(restarted_store)
    restarted_center = PackagedProductCommandCenter(
        products=ProductProjectCommandService(restarted_repository),
        status_reader=PackagedProductFactoryStatusReader(restarted_store),
    )
    restarted_router = _status_router(
        store=restarted_store,
        repository=restarted_repository,
        project_id=project.project_id,
        center=restarted_center,
    )

    after = restarted_router.create({"command": "Show current Product Factory status"})

    assert after == before


def test_factory_status_command_bounds_large_component_summary(tmp_path: Path) -> None:
    store, repository, project, plan, preparation, center = _fixture(tmp_path)
    components = tuple(
        ProductComponent(
            component_id=f"component-{index}",
            repository_id="repo-core",
            paths=(f"src/component-{index}",),
            test_commands=(("python", "-m", "pytest", f"tests/component-{index}"),),
        )
        for index in range(10)
    )
    wide_plan = PackagedProductFactoryExecutionPlan(
        project_id=project.project_id,
        expected_spec_version=project.spec_version,
        expected_row_version=project.row_version,
        graph=ProductRepositoryGraph(
            project_id=project.project_id,
            repositories=plan.graph.repositories,
            components=components,
        ),
        graph_version=1,
        base_shas=dict(plan.base_shas),
        component_goals={
            item.component_id: f"Implement {item.component_id}" for item in components
        },
        permission_ceiling=plan.permission_ceiling,
    )
    preparation.prepare(wide_plan)
    router = _status_router(
        store=store,
        repository=repository,
        project_id=project.project_id,
        center=center,
    )

    result = router.create({"command": "Show current Product Factory status"})

    assert "компонентів 10" in result.message
    assert "показано 8 з 10 компонентів" in result.message
    assert "component-0=ready" in result.message
    assert "component-7=ready" in result.message
    assert "component-8=ready" not in result.message
    assert "component-9=ready" not in result.message


def test_factory_status_command_reports_unprepared_current_version(tmp_path: Path) -> None:
    store, repository, project, _plan, _preparation, center = _fixture(tmp_path)
    router = _status_router(
        store=store,
        repository=repository,
        project_id=project.project_id,
        center=center,
    )

    result = router.create({"command": "Поточний статус Product Factory"})

    assert result.status == "completed"
    assert result.focus_id == "product-project-operator-heading"
    assert result.message == (
        f"Статус Product Factory для {project.project_id}: "
        "поточна версія ProductProject ще не має підготовленого execution authority."
    )


def test_windows_composition_uses_read_only_packaged_factory_status_reader() -> None:
    source = (Path(__file__).resolve().parents[1] / "scripts" / "nika_windows.py").read_text(
        encoding="utf-8"
    )

    assert "PackagedProductFactoryStatusReader(store)" in source
    assert "PackagedProductCommandCenter(" in source
    assert "status_reader=PackagedProductFactoryStatusReader(store)" in source
    assert "team_planner=PackagedProductFactoryTeamPlanner(product_repository)" in source
    assert (
        "product_factory_status_inspector=command_center.inspect_packaged_project"
        in source
    )
