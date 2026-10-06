from __future__ import annotations

import asyncio
import pathlib
import shutil
import subprocess
import sys

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.product_factory_coding_worker_adapter import CodingWorkerComponentAdapter
from nika_core.product_factory_coordinator import ProductFactoryCoordinator, WorkState
from nika_core.product_factory_local_coding import (
    ContainedLocalCodingPolicy,
    ContainedLocalProductFactoryPorts,
    build_contained_local_coding_program,
)
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from nika_core.product_factory_packaged_preparation import (
    PackagedProductFactoryExecutionPlan,
    PackagedProductFactoryPreparationService,
)
from nika_core.product_factory_program_host import ProgramWorkDisposition
from nika_core.product_project import ProductProjectRepository, ProductProjectSpec
from nika_core.toolsmith.contracts import ResourceBudget
from nika_core.toolsmith.local_worker import (
    ContainedLocalCodingWorker,
    LocalCodingPlan,
    LocalFileEdit,
)

PERMISSIONS = frozenset({"read_source", "write_source", "run_tests"})


def _git(root: pathlib.Path, *args: str) -> str:
    result = subprocess.run(
        ("git", *args),
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


def _repository(tmp_path: pathlib.Path) -> tuple[pathlib.Path, str]:
    if shutil.which("git") is None:
        pytest.skip("Git CLI unavailable")
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.name", "Nika Test")
    _git(root, "config", "user.email", "nika@example.invalid")
    source = root / "src"
    source.mkdir()
    (source / "core.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "add", "src/core.py")
    _git(root, "commit", "-m", "base")
    return root, _git(root, "rev-parse", "HEAD")


class _Planner:
    def __init__(self) -> None:
        self.calls = 0

    async def plan(self, _job):
        self.calls += 1
        return LocalCodingPlan((LocalFileEdit("src/core.py", b"VALUE = 2\n"),))


def _graph(base_command: tuple[str, ...]) -> ProductRepositoryGraph:
    return ProductRepositoryGraph(
        project_id="project-1",
        repositories=(
            RepositoryRef(
                repository_id="repo-1",
                provider="github",
                locator="org/repo",
                default_branch="main",
            ),
        ),
        components=(
            ProductComponent(
                component_id="core",
                repository_id="repo-1",
                paths=("src",),
                test_commands=(base_command,),
            ),
        ),
    )


def _run(coroutine):
    return asyncio.run(coroutine)


def test_product_factory_adapter_reaches_real_private_candidate_and_review_gate(
    tmp_path: pathlib.Path,
) -> None:
    repository, base_sha = _repository(tmp_path)
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    python = str(pathlib.Path(sys.executable).resolve(strict=True))
    planner = _Planner()
    worker = ContainedLocalCodingWorker(
        workspace_parent=jobs,
        repositories={"repo-1": repository},
        planner=planner,
    )
    policy = ContainedLocalCodingPolicy(
        allowed_executables=(python,),
        resource_budget=ResourceBudget(20, 1024 * 1024, 10),
    )
    ports = ContainedLocalProductFactoryPorts(worker, policy)
    adapter = CodingWorkerComponentAdapter(worker, ports, ports)
    coordinator = ProductFactoryCoordinator(
        _graph(
            (
                python,
                "-c",
                "from pathlib import Path; "
                "assert Path('src/core.py').read_text() == 'VALUE = 2\\n'",
            )
        )
    )
    coordinator.plan(
        base_shas={"repo-1": base_sha},
        goals={"core": "update core"},
        permission_ceiling=PERMISSIONS,
    )

    record = _run(adapter.run_component(coordinator, "core"))

    assert record.state is WorkState.REVIEW_REQUIRED
    assert record.result is not None
    assert record.result.base_sha == base_sha
    assert record.result.result_sha != base_sha
    assert len(record.result.diff_digest) == 64
    assert record.result.coding_result.failure is None
    assert [item.path for item in record.result.coding_result.changed_files] == [
        "src/core.py"
    ]
    assert planner.calls == 1
    assert (repository / "src" / "core.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    candidate = worker.candidate_worktree(record.request.work_id)
    assert (candidate / "src" / "core.py").read_text(encoding="utf-8") == "VALUE = 2\n"


def test_production_builder_reuses_canonical_program_host_and_same_ports(
    tmp_path: pathlib.Path,
) -> None:
    repository, _base_sha = _repository(tmp_path)
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    python = str(pathlib.Path(sys.executable).resolve(strict=True))
    planner = _Planner()

    program = build_contained_local_coding_program(
        store,
        workspace_parent=jobs,
        repositories={"repo-1": repository},
        planner=planner,
        policy=ContainedLocalCodingPolicy(
            allowed_executables=(python,),
            resource_budget=ResourceBudget(20, 1024 * 1024, 10),
        ),
    )

    assert isinstance(program.host.worker, CodingWorkerComponentAdapter)
    assert program.host.worker.worker is program.worker
    assert program.host.worker.contexts is program.ports
    assert program.host.worker.evidence is program.ports
    assert program.multi_repository_host.store is store
    assert program.multi_repository_host.worker is program.host.worker
    assert program.multi_repository_host._program is program.host
    assert program.multi_repository_host._program._ledger is program.host._ledger
    assert program.multi_repository_host._program._ownership is program.host._ownership


def test_contained_local_multi_repository_host_drives_packaged_prepare_and_dispatch(
    tmp_path: pathlib.Path,
) -> None:
    repository, base_sha = _repository(tmp_path)
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    python = str(pathlib.Path(sys.executable).resolve(strict=True))
    planner = _Planner()
    program = build_contained_local_coding_program(
        store,
        workspace_parent=jobs,
        repositories={"repo-1": repository},
        planner=planner,
        policy=ContainedLocalCodingPolicy(
            allowed_executables=(python,),
            resource_budget=ResourceBudget(20, 1024 * 1024, 10),
        ),
    )

    projects = ProductProjectRepository(store)
    locator = "org/repo"
    project = projects.create(
        project_id="product-contained-local-packaged",
        name="Contained local packaged Product Factory",
        spec=ProductProjectSpec(
            goal="Build one isolated tested local component",
            desired_outcome="A private reviewed candidate exists",
            repository_refs=(locator,),
        ),
        idempotency_key="create:product-contained-local-packaged",
    )
    graph = ProductRepositoryGraph(
        project_id=project.project_id,
        repositories=(
            RepositoryRef(
                repository_id="repo-1",
                provider="github",
                locator=locator,
                default_branch="main",
            ),
        ),
        components=(
            ProductComponent(
                component_id="core",
                repository_id="repo-1",
                paths=("src",),
                test_commands=(
                    (
                        python,
                        "-c",
                        "from pathlib import Path; "
                        "assert Path('src/core.py').read_text() == 'VALUE = 2\n'",
                    ),
                ),
            ),
        ),
    )
    plan = PackagedProductFactoryExecutionPlan(
        project_id=project.project_id,
        expected_spec_version=project.spec_version,
        expected_row_version=project.row_version,
        graph=graph,
        graph_version=1,
        base_shas={"repo-1": base_sha},
        component_goals={"core": "update core without publishing"},
        permission_ceiling=PERMISSIONS,
    )
    service = PackagedProductFactoryPreparationService(
        repository=projects,
        tasks=TaskQueue(store),
        host=program.multi_repository_host,
        workspace_id="packaged.product-factory",
    )

    prepared = service.prepare(plan)
    outcomes = _run(
        program.multi_repository_host.dispatch_ready(
            host_task_id=prepared.host_task_id,
            state=prepared.state,
            max_parallel=1,
            max_count=1,
        )
    )

    assert len(outcomes) == 1
    assert outcomes[0].disposition is ProgramWorkDisposition.REVIEW_REQUIRED
    assert planner.calls == 1
    assert (repository / "src" / "core.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    work_id = prepared.state.coordinator.snapshot().records[0].request.work_id
    candidate = program.worker.candidate_worktree(work_id)
    assert (candidate / "src" / "core.py").read_text(encoding="utf-8") == "VALUE = 2\n"


def test_policy_rejects_relative_or_noncanonical_executable_identity() -> None:
    with pytest.raises(ValueError, match="absolute paths"):
        ContainedLocalCodingPolicy(
            allowed_executables=("python",),
            resource_budget=ResourceBudget(20, 1024, 5),
        )

    python = pathlib.Path(sys.executable)
    resolved = python.resolve(strict=True)
    if str(python) != str(resolved):
        with pytest.raises(ValueError, match="already be canonical"):
            ContainedLocalCodingPolicy(
                allowed_executables=(str(python),),
                resource_budget=ResourceBudget(20, 1024, 5),
            )