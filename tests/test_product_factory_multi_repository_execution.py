from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_checkpoint_host import (
    ProductFactoryCheckpointError,
    ProductFactoryCheckpointHost,
)
from nika_core.product_factory_coding_worker_adapter import (
    CodingWorkerComponentAdapter,
    CodingWorkerDispatchContext,
    CodingWorkerExecutionEvidence,
    RepositoryPathIdentity,
)
from nika_core.product_factory_coordinator import ReviewDecision, WorkState
from nika_core.product_factory_multi_repository import (
    MultiRepositoryExecutionError,
    MultiRepositoryProductFactoryHost,
    RepairLineageError,
    RepositoryGraphIntegrityError,
)
from nika_core.product_factory_orchestration import (
    ComponentBrief,
    DynamicTeamComposer,
    OwnershipLease,
    ProductComponent,
    ProductRepositoryGraph,
    ProjectScale,
    RepositoryGraphError,
    RepositoryRef,
    TeamCompositionRequest,
)
from nika_core.product_factory_program_host import ProgramWorkDisposition
from nika_core.product_project import (
    ProductProjectRepository,
    ProductProjectSpec,
    ProductRequirement,
)
from nika_core.toolsmith.contracts import (
    ChangedFile,
    CodingJob,
    CodingResult,
    CodingWorkerPort,
    IsolationClass,
    NetworkPolicy,
    ProcessPolicy,
    RecoveryState,
    ResourceBudget,
    TestEvidence,
    WorkerFailure,
    WorkerFailureKind,
    WorkspaceLease,
)

PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})
GRAPH_VERSION = 7


@dataclass(frozen=True, slots=True)
class LocalRepository:
    repository_id: str
    path: Path
    base_sha: str
    tree_sha: str


@dataclass(slots=True)
class Fixture:
    db_path: Path
    project_id: str
    task_id: str
    graph: ProductRepositoryGraph
    repositories: dict[str, LocalRepository]
    worker: DeterministicMultiRepoWorker


class DeterministicMultiRepoWorker(CodingWorkerPort):
    """Local deterministic worker fake behind the real CodingWorker adapter."""

    def __init__(self) -> None:
        self.active = 0
        self.peak_active = 0
        self.executions: dict[str, int] = {}
        self.fail_first: set[str] = {"assets"}
        self.outside_scope: set[str] = set()
        self.recovery: dict[str, RecoveryState] = {}

    async def execute(self, job: CodingJob) -> CodingResult:
        component_id = job.task_id.rsplit(":", 1)[-1]
        self.executions[component_id] = self.executions.get(component_id, 0) + 1
        ordinal = self.executions[component_id]
        self.active += 1
        self.peak_active = max(self.peak_active, self.active)
        try:
            await asyncio.sleep(0.02)
            root = job.allowed_paths.roots[0]
            changed_path = (
                "src/not-owned/hijack.py"
                if component_id in self.outside_scope
                else f"{root}/worker-output-{ordinal}.py"
            )
            changed = ChangedFile(
                path=changed_path,
                sha256=_digest(f"changed:{job.job_id}:{changed_path}"),
                size_bytes=32,
            )
            if component_id in self.fail_first and ordinal == 1:
                return CodingResult(
                    job_id=job.job_id,
                    changed_files=(changed,),
                    failure=WorkerFailure(
                        WorkerFailureKind.PROCESS_FAILED,
                        "deterministic first-attempt failure",
                        retryable=True,
                    ),
                )
            return CodingResult(
                job_id=job.job_id,
                changed_files=(changed,),
                test_evidence=tuple(
                    TestEvidence(
                        command=command.argv,
                        exit_code=0,
                        output_digest=_digest(
                            f"test:{job.job_id}:{' '.join(command.argv)}"
                        ),
                    )
                    for command in job.acceptance_commands
                ),
            )
        finally:
            self.active -= 1

    async def cancel(self, job_id: str) -> None:
        self.recovery[job_id] = RecoveryState("cancelled")

    async def inspect(self, job_id: str) -> RecoveryState | None:
        return self.recovery.get(job_id)

    async def recover(self, job: CodingJob, state: RecoveryState) -> CodingResult:
        self.recovery[job.job_id] = state
        return await self.execute(job)


class LocalContextPort:
    def __init__(self, repositories: dict[str, LocalRepository]) -> None:
        self.repositories = repositories

    async def context_for(self, request):
        repository = self.repositories[request.repository_id]
        return CodingWorkerDispatchContext(
            repository_tree_digest=repository.tree_sha,
            lease=WorkspaceLease(
                lease_id=f"workspace:{request.work_id}",
                workspace_root=repository.path,
                isolation_class=IsolationClass.POLICY_ONLY,
                expires_at="2099-01-01T00:00:00+00:00",
            ),
            process_policy=ProcessPolicy(("python",)),
            network_policy=NetworkPolicy(),
            resource_budget=ResourceBudget(
                timeout_seconds=30,
                max_output_bytes=200_000,
                max_changed_files=16,
            ),
            path_identity=RepositoryPathIdentity.CASE_SENSITIVE,
        )


class DeterministicEvidencePort:
    async def collect(
        self,
        request,
        job: CodingJob,
        result: CodingResult,
    ) -> CodingWorkerExecutionEvidence:
        del job
        return CodingWorkerExecutionEvidence(
            work_id=request.work_id,
            repository_id=request.repository_id,
            base_sha=request.base_sha,
            result_sha=_sha(f"result:{request.work_id}"),
            diff_digest=_digest(
                f"diff:{request.work_id}:"
                + ",".join(item.path for item in result.changed_files)
            ),
        )


def _run(coroutine):
    return asyncio.run(coroutine)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha(value: str) -> str:
    return _digest(value)[:40]


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={
            **os.environ,
            "GIT_AUTHOR_DATE": "2026-08-23T00:00:00+00:00",
            "GIT_COMMITTER_DATE": "2026-08-23T00:00:00+00:00",
        },
    )
    return result.stdout.strip()


def _create_git_repository(
    root: Path,
    *,
    repository_id: str,
    component_id: str,
) -> LocalRepository:
    path = root / f"{repository_id} real repo"
    path.mkdir(parents=True)
    _git(path, "init", "--initial-branch=main")
    _git(path, "config", "user.name", "Nika Deterministic Fixture")
    _git(path, "config", "user.email", "nika-fixture@example.invalid")
    source_dir = path / "src" / component_id
    tests_dir = path / "tests"
    source_dir.mkdir(parents=True)
    tests_dir.mkdir()
    (source_dir / "__init__.py").write_text(
        f'VALUE = "{component_id}"\n',
        encoding="utf-8",
    )
    (tests_dir / "test_smoke.py").write_text(
        f'def test_{component_id}_fixture():\n'
        f'    assert "{component_id}".strip()\n',
        encoding="utf-8",
    )
    _git(path, "add", ".")
    _git(path, "commit", "-m", f"fixture: initialize {component_id}")
    return LocalRepository(
        repository_id=repository_id,
        path=path,
        base_sha=_git(path, "rev-parse", "HEAD"),
        tree_sha=_git(path, "rev-parse", "HEAD^{tree}"),
    )


def _build_fixture(tmp_path: Path) -> Fixture:
    repositories = {
        repository_id: _create_git_repository(
            tmp_path,
            repository_id=repository_id,
            component_id=component_id,
        )
        for repository_id, component_id in (
            ("repo-api", "api"),
            ("repo-assets", "assets"),
            ("repo-sdk", "sdk"),
            ("repo-desktop", "desktop"),
        )
    }
    graph = ProductRepositoryGraph(
        project_id="project-multi-repo",
        repositories=tuple(
            RepositoryRef(
                repository_id=item.repository_id,
                provider="local-git",
                locator=str(item.path.resolve()),
                default_branch="main",
                case_sensitive_paths=True,
            )
            for item in repositories.values()
        ),
        components=(
            ProductComponent(
                component_id="api",
                repository_id="repo-api",
                paths=("src/api",),
                build_commands=(("python", "-m", "compileall", "src"),),
                test_commands=(("python", "-m", "pytest", "tests"),),
                release_identity="api-v1",
            ),
            ProductComponent(
                component_id="assets",
                repository_id="repo-assets",
                paths=("src/assets",),
                build_commands=(("python", "-m", "compileall", "src"),),
                test_commands=(("python", "-m", "pytest", "tests"),),
                release_identity="assets-v1",
            ),
            ProductComponent(
                component_id="sdk",
                repository_id="repo-sdk",
                paths=("src/sdk",),
                dependencies=("api",),
                build_commands=(("python", "-m", "compileall", "src"),),
                test_commands=(("python", "-m", "pytest", "tests"),),
                release_identity="sdk-v1",
            ),
            ProductComponent(
                component_id="desktop",
                repository_id="repo-desktop",
                paths=("src/desktop",),
                dependencies=("assets", "sdk"),
                build_commands=(("python", "-m", "compileall", "src"),),
                test_commands=(("python", "-m", "pytest", "tests"),),
                release_identity="desktop-v1",
            ),
        ),
    )

    store = SQLiteStore(tmp_path / "nika-multi-repo.db")
    store.initialize()
    project = ProductProjectRepository(store).create(
        project_id=graph.project_id,
        name="Deterministic Multi Repository Product",
        spec=ProductProjectSpec(
            goal="Build a dependency-ordered product across four repositories",
            desired_outcome="All repository-owned components independently reviewed",
            requirements=(
                ProductRequirement(
                    requirement_id="multi-repo",
                    text="Components remain inside exact repository ownership",
                    acceptance=(
                        "Independent repositories progress in parallel",
                        "Dependency integration order is preserved",
                        "Restart restores the exact repository graph",
                    ),
                ),
            ),
            repository_refs=tuple(
                repository.locator for repository in graph.repositories
            ),
        ),
        idempotency_key="create:project-multi-repo",
    )
    task = TaskQueue(store).create(
        workspace_id="ws-product-factory",
        agent_id="product-factory",
        payload={
            "kind": "product_factory",
            "product_project_id": project.project_id,
        },
    )
    worker = DeterministicMultiRepoWorker()
    host = _host(store, repositories, worker)
    host.initialize(
        host_task_id=task.task_id,
        project=project,
        graph=graph,
        graph_version=GRAPH_VERSION,
        base_shas={
            repository_id: repository.base_sha
            for repository_id, repository in repositories.items()
        },
        component_goals={
            component.component_id: f"Implement {component.component_id}"
            for component in graph.components
        },
        permission_ceiling=PERMISSIONS,
    )
    return Fixture(
        db_path=store.path,
        project_id=project.project_id,
        task_id=task.task_id,
        graph=graph,
        repositories=repositories,
        worker=worker,
    )


def _host(
    store: SQLiteStore,
    repositories: dict[str, LocalRepository],
    worker: DeterministicMultiRepoWorker,
) -> MultiRepositoryProductFactoryHost:
    adapter = CodingWorkerComponentAdapter(
        worker=worker,
        contexts=LocalContextPort(repositories),
        evidence=DeterministicEvidencePort(),
    )
    return MultiRepositoryProductFactoryHost(store, adapter)


def _restart(fixture: Fixture):
    store = SQLiteStore(fixture.db_path)
    store.initialize()
    project = ProductProjectRepository(store).get(fixture.project_id)
    host = _host(store, fixture.repositories, fixture.worker)
    state = host.restore(host_task_id=fixture.task_id, project=project)
    return store, host, state


def _record(state, component_id: str):
    return next(
        record
        for record in state.coordinator.snapshot().records
        if record.request.component_id == component_id
    )


def _accept(host, state, task_id: str, component_id: str) -> None:
    host.review_and_checkpoint(
        host_task_id=task_id,
        state=state,
        component_id=component_id,
        decision=ReviewDecision(
            reviewer_id=f"independent-qa:{component_id}",
            accepted=True,
            reason=f"{component_id} exact evidence accepted",
            evidence_refs=(f"qa:{component_id}",),
        ),
    )


def test_real_four_repository_vertical_parallel_failure_repair_and_long_restart(
    tmp_path: Path,
) -> None:
    fixture = _build_fixture(tmp_path)
    _, host, state = _restart(fixture)

    assert len(state.authority.graph.repositories) == 4
    assert state.authority.graph_version == GRAPH_VERSION
    assert state.authority.graph.dependency_order() == (
        "api",
        "assets",
        "sdk",
        "desktop",
    )
    assert {
        (edge.component_id, edge.depends_on_component_id, edge.graph_version)
        for edge in state.authority.dependency_edges
    } == {
        ("sdk", "api", GRAPH_VERSION),
        ("desktop", "assets", GRAPH_VERSION),
        ("desktop", "sdk", GRAPH_VERSION),
    }
    for repository in fixture.repositories.values():
        assert len(repository.base_sha) == 40
        assert _git(repository.path, "rev-parse", "HEAD") == repository.base_sha

    team = DynamicTeamComposer().compose(
        TeamCompositionRequest(
            project_id=fixture.project_id,
            components=(
                ComponentBrief("api", "backend"),
                ComponentBrief("assets", "data"),
                ComponentBrief("sdk", "backend"),
                ComponentBrief("desktop", "windows"),
            ),
            acceptance_criteria=(
                "independent review",
                "accessible Windows release",
            ),
            permission_ceiling=PERMISSIONS,
            scale=ProjectScale.LARGE,
        )
    )
    implementation_roles = [
        role
        for role in team.roles
        if role.capabilities == ("implementation",)
    ]
    assert {role.component_ids for role in implementation_roles} == {
        ("api",),
        ("assets",),
        ("sdk",),
        ("desktop",),
    }
    assert all(role.permissions <= PERMISSIONS for role in team.roles)

    wave_one = _run(
        host.dispatch_ready(
            host_task_id=fixture.task_id,
            state=state,
            max_parallel=4,
        )
    )
    by_component = {item.component_id: item for item in wave_one}
    assert by_component["api"].disposition is ProgramWorkDisposition.REVIEW_REQUIRED
    assert by_component["assets"].disposition is ProgramWorkDisposition.REPAIR_REQUIRED
    assert fixture.worker.peak_active >= 2
    assert _record(state, "api").state is WorkState.REVIEW_REQUIRED
    assert _record(state, "assets").state is WorkState.REPAIR_REQUIRED
    assert _record(state, "sdk").state is WorkState.PLANNED
    assert _record(state, "desktop").state is WorkState.PLANNED
    failed_assets_sha = _record(state, "assets").result.result_sha

    _, host, state = _restart(fixture)
    _accept(host, state, fixture.task_id, "api")
    assert _record(state, "sdk").state is WorkState.READY
    assert _record(state, "desktop").state is WorkState.PLANNED

    _, host, state = _restart(fixture)
    repair_request, lineage = host.prepare_repair_and_checkpoint(
        host_task_id=fixture.task_id,
        state=state,
        component_id="assets",
        reason="repair deterministic asset failure",
    )
    assert lineage.from_result_sha == failed_assets_sha
    assert lineage.to_base_sha == failed_assets_sha
    assert repair_request.base_sha == failed_assets_sha
    assert repair_request.attempt == 2

    _, host, state = _restart(fixture)
    conflict_a = OwnershipLease(
        lease_id="external:a",
        worker_id="external-worker-a",
        component_ids=("assets",),
        allowed_paths=("src/assets",),
    )
    conflict_b = OwnershipLease(
        lease_id="external:b",
        worker_id="external-worker-b",
        component_ids=("assets",),
        allowed_paths=("src/assets",),
    )
    with pytest.raises(MultiRepositoryExecutionError) as first_conflict:
        _run(
            host.dispatch_ready(
                host_task_id=fixture.task_id,
                state=state,
                active_leases=(conflict_b, conflict_a),
            )
        )
    with pytest.raises(MultiRepositoryExecutionError) as second_conflict:
        _run(
            host.dispatch_ready(
                host_task_id=fixture.task_id,
                state=state,
                active_leases=(conflict_a, conflict_b),
            )
        )
    assert str(first_conflict.value) == str(second_conflict.value)
    assert fixture.worker.executions["assets"] == 1
    assert fixture.worker.executions.get("sdk", 0) == 0

    wave_two = _run(
        host.dispatch_ready(
            host_task_id=fixture.task_id,
            state=state,
            max_parallel=4,
        )
    )
    assert {item.component_id for item in wave_two} == {"assets", "sdk"}
    assert all(
        item.disposition is ProgramWorkDisposition.REVIEW_REQUIRED
        for item in wave_two
    )
    assert fixture.worker.executions["assets"] == 2
    assert fixture.worker.executions["sdk"] == 1

    _, host, state = _restart(fixture)
    _accept(host, state, fixture.task_id, "sdk")
    assert _record(state, "desktop").state is WorkState.PLANNED
    _accept(host, state, fixture.task_id, "assets")
    assert _record(state, "desktop").state is WorkState.READY

    _, host, state = _restart(fixture)
    final_wave = _run(
        host.dispatch_ready(
            host_task_id=fixture.task_id,
            state=state,
            max_parallel=4,
        )
    )
    assert [item.component_id for item in final_wave] == ["desktop"]
    assert final_wave[0].disposition is ProgramWorkDisposition.REVIEW_REQUIRED
    _accept(host, state, fixture.task_id, "desktop")
    final_snapshot = state.coordinator.snapshot()
    final_graph_digest = state.authority.graph_digest
    final_edges = state.authority.dependency_edges

    for _ in range(30):
        _, host, state = _restart(fixture)
        assert state.authority.graph_digest == final_graph_digest
        assert state.authority.dependency_edges == final_edges
        assert state.coordinator.snapshot() == final_snapshot
        assert all(
            record.state is WorkState.ACCEPTED
            for record in state.coordinator.snapshot().records
        )
        durable_lineage = host.repair_lineage(
            host_task_id=fixture.task_id,
            state=state,
        )
        assert durable_lineage == (lineage,)


def test_out_of_scope_worker_evidence_isolated_from_sibling_repository(
    tmp_path: Path,
) -> None:
    fixture = _build_fixture(tmp_path)
    fixture.worker.fail_first.clear()
    fixture.worker.outside_scope.add("api")
    _, host, state = _restart(fixture)

    outcomes = _run(
        host.dispatch_ready(
            host_task_id=fixture.task_id,
            state=state,
            max_parallel=4,
        )
    )
    by_component = {item.component_id: item for item in outcomes}
    assert by_component["api"].disposition is ProgramWorkDisposition.UNCERTAIN
    assert by_component["assets"].disposition is ProgramWorkDisposition.REVIEW_REQUIRED
    assert _record(state, "api").state is WorkState.RUNNING
    assert _record(state, "assets").state is WorkState.REVIEW_REQUIRED
    assert _record(state, "sdk").state is WorkState.PLANNED


def test_identical_physical_repository_cannot_use_two_logical_repository_ids(
    tmp_path: Path,
) -> None:
    repository = _create_git_repository(
        tmp_path,
        repository_id="shared",
        component_id="one",
    )
    locator = str(repository.path.resolve())
    with pytest.raises(RepositoryGraphError, match="aliased by multiple repository ids"):
        ProductRepositoryGraph(
            project_id="project-alias",
            repositories=(
                RepositoryRef("repo-one", "local-git", locator, "main"),
                RepositoryRef("repo-two", "local-git", locator, "main"),
            ),
            components=(
                ProductComponent("one", "repo-one", ("src/one",)),
                ProductComponent("two", "repo-two", ("src/two",)),
            ),
        )


def test_repository_graph_authority_rejects_recomputed_checkpoint_tamper(
    tmp_path: Path,
) -> None:
    fixture = _build_fixture(tmp_path)
    store, _, _ = _restart(fixture)
    with store.connection() as conn:
        row = conn.execute(
            """
            SELECT checkpoint_id, payload_json
            FROM checkpoints
            WHERE task_id = ? AND stage = 'product_factory.repository_graph.v1'
            """,
            (fixture.task_id,),
        ).fetchone()
        assert row is not None
        payload = json.loads(row["payload_json"])
        payload["graph_version"] = GRAPH_VERSION + 1
        for edge in payload["dependency_edges"]:
            edge["graph_version"] = GRAPH_VERSION + 1
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        conn.execute(
            """
            UPDATE checkpoints
            SET payload_json = ?, checksum_sha256 = ?
            WHERE checkpoint_id = ?
            """,
            (
                canonical,
                hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                row["checkpoint_id"],
            ),
        )

    store = SQLiteStore(fixture.db_path)
    store.initialize()
    project = ProductProjectRepository(store).get(fixture.project_id)
    host = _host(store, fixture.repositories, fixture.worker)
    with pytest.raises(RepositoryGraphIntegrityError, match="host task repository graph authority"):
        host.restore(host_task_id=fixture.task_id, project=project)


def test_low_level_repair_without_explicit_lineage_fails_closed_on_restart(
    tmp_path: Path,
) -> None:
    fixture = _build_fixture(tmp_path)
    _, host, state = _restart(fixture)
    _run(
        host.dispatch_ready(
            host_task_id=fixture.task_id,
            state=state,
            max_parallel=4,
        )
    )
    failed = _record(state, "assets")
    assert failed.state is WorkState.REPAIR_REQUIRED
    state.coordinator.prepare_repair(
        "assets",
        base_sha="f" * 40,
        reason="bypass high-level repair lineage",
    )
    try:
        ProductFactoryCheckpointHost(host.store).save(
            host_task_id=fixture.task_id,
            checkpoint=state.binding.checkpoint(state.coordinator),
        )
    except ProductFactoryCheckpointError:
        # Future checkpoint hardening may reject the bypass even earlier.
        return

    store = SQLiteStore(fixture.db_path)
    store.initialize()
    project = ProductProjectRepository(store).get(fixture.project_id)
    restarted = _host(store, fixture.repositories, fixture.worker)
    with pytest.raises(RepairLineageError, match="advanced without durable repair lineage"):
        restarted.restore(host_task_id=fixture.task_id, project=project)
