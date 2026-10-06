from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_local_repository_binding import (
    ProductFactoryLocalRepositoryBindingError,
    ProductFactoryLocalRepositoryBindings,
)
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from nika_core.product_factory_packaged_bound_local_host import (
    PackagedBoundLocalProductFactoryHost,
    PackagedBoundLocalProductFactoryHostError,
)
from nika_core.product_factory_packaged_local_startup import (
    build_packaged_local_product_factory_program,
    decode_packaged_local_product_factory_startup,
)
from nika_core.product_project import ProductProjectRepository, ProductProjectSpec
from nika_core.toolsmith.local_worker import LocalCodingPlan, LocalFileEdit
from nika_core.v01_model_settings import V01ModelSettings


def _git(root: pathlib.Path, *args: str) -> str:
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git CLI unavailable")
    result = subprocess.run(
        (executable, *args),
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


def _repository(tmp_path: pathlib.Path, name: str) -> pathlib.Path:
    root = tmp_path / name
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.name", "Nika Test")
    _git(root, "config", "user.email", "nika@example.invalid")
    (root / "app.py").write_text("print('base')\n", encoding="utf-8")
    _git(root, "add", "app.py")
    _git(root, "commit", "-m", "base")
    return root.resolve()


def _store(tmp_path: pathlib.Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "стан Ніки" / "nika.db")
    store.initialize()
    return store


def _repository_ref() -> RepositoryRef:
    return RepositoryRef(
        repository_id="repo-1",
        provider="github",
        locator="Oleksii-debug/example",
        default_branch="main",
    )


def _project(store: SQLiteStore, repository: RepositoryRef):
    return ProductProjectRepository(store).create(
        project_id="product-1",
        name="Product 1",
        spec=ProductProjectSpec(
            goal="Build the product",
            desired_outcome="Verified package",
            repository_refs=(repository.locator,),
        ),
        idempotency_key="create:product-1",
    )


def _graph(
    project_id: str,
    repository: RepositoryRef,
) -> ProductRepositoryGraph:
    return ProductRepositoryGraph(
        project_id=project_id,
        repositories=(repository,),
        components=(
            ProductComponent(
                component_id="core",
                repository_id=repository.repository_id,
                paths=("src",),
            ),
        ),
    )


def _startup(tmp_path: pathlib.Path):
    executable = shutil.which("git")
    if executable is None:
        pytest.skip("Git CLI unavailable")
    workspace = tmp_path / "factory jobs"
    workspace.mkdir(exist_ok=True)
    raw = json.dumps(
        {
            "schema": "nika.product-factory.local-startup.v2",
            "workspace_parent": str(workspace.resolve()),
            "allowed_executables": [
                str(pathlib.Path(executable).resolve())
            ],
            "resource_budget": {
                "timeout_seconds": 30,
                "max_output_bytes": 1024 * 1024,
                "max_changed_files": 20,
            },
            "lease_seconds": 300,
            "git_executable": str(pathlib.Path(executable).resolve()),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    startup = decode_packaged_local_product_factory_startup(raw)
    assert startup is not None
    return startup


def _settings(store: SQLiteStore) -> V01ModelSettings:
    settings = V01ModelSettings(store)
    result = settings.configure(
        {
            "schema_version": 1,
            "revision": 0,
            "route_kind": "ollama",
            "provider_id": "ollama",
            "model": "qwen3:8b",
            "base_url": "http://localhost:11434",
            "credential_ref": None,
            "private_data_allowed": False,
            "timeout_seconds": 60.0,
        }
    )
    assert result.status == "completed"
    return settings


def test_dynamic_host_builds_worker_only_from_durable_repository_binding(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository_ref()
    project = _project(store, repository)
    durable_root = _repository(tmp_path, "durable repository")
    bindings = ProductFactoryLocalRepositoryBindings(store)
    bound = bindings.bind(
        project_id=project.project_id,
        repository=repository,
        root=durable_root,
        expected_binding_version=None,
    )
    program = build_packaged_local_product_factory_program(
        store,
        settings=_settings(store),
        startup=_startup(tmp_path),
    )
    host = program.multi_repository_host

    resolved = host._bindings_for_graph(
        project,
        _graph(project.project_id, repository),
    )
    entry = host._entry_for("host-task", project, resolved)

    assert resolved == {repository.repository_id: bound}
    assert entry.bindings[repository.repository_id].binding_version == 1
    assert entry.program.worker.repositories == {
        repository.repository_id: durable_root,
    }


def test_rebind_after_host_composition_fails_closed_before_worker_reuse(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository_ref()
    project = _project(store, repository)
    first_root = _repository(tmp_path, "first repository")
    second_root = _repository(tmp_path, "second repository")
    bindings = ProductFactoryLocalRepositoryBindings(store)
    first = bindings.bind(
        project_id=project.project_id,
        repository=repository,
        root=first_root,
        expected_binding_version=None,
    )
    host = PackagedBoundLocalProductFactoryHost(
        store,
        settings=_settings(store),
        startup=_startup(tmp_path),
        bindings=bindings,
    )
    initial = host._bindings_for_graph(
        project,
        _graph(project.project_id, repository),
    )
    host._entry_for("host-task", project, initial)

    second = bindings.bind(
        project_id=project.project_id,
        repository=repository,
        root=second_root,
        expected_binding_version=first.binding_version,
    )
    changed = host._bindings_for_graph(
        project,
        _graph(project.project_id, repository),
    )

    assert second.binding_version == 2
    with pytest.raises(
        PackagedBoundLocalProductFactoryHostError,
        match="changed after host composition",
    ):
        host._entry_for("host-task", changed)


def test_fresh_host_after_restart_uses_latest_durable_binding(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository_ref()
    project = _project(store, repository)
    first_root = _repository(tmp_path, "first repository")
    second_root = _repository(tmp_path, "second repository")
    bindings = ProductFactoryLocalRepositoryBindings(store)
    first = bindings.bind(
        project_id=project.project_id,
        repository=repository,
        root=first_root,
        expected_binding_version=None,
    )
    second = bindings.bind(
        project_id=project.project_id,
        repository=repository,
        root=second_root,
        expected_binding_version=first.binding_version,
    )

    reopened_store = SQLiteStore(store.path)
    reopened_store.initialize()
    reopened_settings = V01ModelSettings(reopened_store)
    host = PackagedBoundLocalProductFactoryHost(
        reopened_store,
        settings=reopened_settings,
        startup=_startup(tmp_path),
    )
    reopened_project = ProductProjectRepository(reopened_store).get(
        project.project_id
    )
    current = host._bindings_for_project(reopened_project)
    entry = host._entry_for("restarted-host-task", reopened_project, current)

    assert (
        current[repository.repository_id].binding_version
        == second.binding_version
    )
    assert current[repository.repository_id].root == second_root
    assert entry.program.worker.repositories == {
        repository.repository_id: second_root,
    }


def test_dynamic_host_refuses_repository_graph_locator_substitution(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository_ref()
    project = _project(store, repository)
    bindings = ProductFactoryLocalRepositoryBindings(store)
    bindings.bind(
        project_id=project.project_id,
        repository=repository,
        root=_repository(tmp_path, "durable repository"),
        expected_binding_version=None,
    )
    host = PackagedBoundLocalProductFactoryHost(
        store,
        settings=_settings(store),
        startup=_startup(tmp_path),
        bindings=bindings,
    )
    substituted = RepositoryRef(
        repository_id=repository.repository_id,
        provider=repository.provider,
        locator="Oleksii-debug/substituted",
        default_branch="main",
    )

    with pytest.raises(
        PackagedBoundLocalProductFactoryHostError,
        match="does not match repository graph",
    ):
        host._bindings_for_graph(
            project,
            _graph(project.project_id, substituted),
        )


def test_dynamic_host_does_not_infer_missing_local_binding(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository_ref()
    project = _project(store, repository)
    host = PackagedBoundLocalProductFactoryHost(
        store,
        settings=_settings(store),
        startup=_startup(tmp_path),
    )

    assert host._bindings_for_project(project) == {}
    with pytest.raises(ProductFactoryLocalRepositoryBindingError):
        host._bindings_for_graph(
            project,
            _graph(project.project_id, repository),
        )


@pytest.mark.asyncio
async def test_entry_ports_revalidate_binding_after_host_check_before_effect(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository_ref()
    project = _project(store, repository)
    first_root = _repository(tmp_path, "first repository")
    second_root = _repository(tmp_path, "second repository")
    base_sha = _git(first_root, "rev-parse", "HEAD")
    bindings = ProductFactoryLocalRepositoryBindings(store)
    first = bindings.bind(
        project_id=project.project_id,
        repository=repository,
        root=first_root,
        expected_binding_version=None,
    )
    host = PackagedBoundLocalProductFactoryHost(
        store,
        settings=_settings(store),
        startup=_startup(tmp_path),
        bindings=bindings,
    )
    state = host.initialize(
        host_task_id="host-task",
        project=project,
        graph=_graph(project.project_id, repository),
        graph_version=1,
        base_shas={repository.repository_id: base_sha},
        component_goals={"core": "Implement core"},
        permission_ceiling=frozenset({"read_source", "write_source", "run_tests"}),
    )
    entry = host._require_state_bindings("host-task", state)
    request = state.coordinator.snapshot().records[0].request

    second = bindings.bind(
        project_id=project.project_id,
        repository=repository,
        root=second_root,
        expected_binding_version=first.binding_version,
    )
    assert second.binding_version == first.binding_version + 1

    with pytest.raises(
        PackagedBoundLocalProductFactoryHostError,
        match="changed during contained-local execution",
    ):
        await entry.program.ports.context_for(request)
    with pytest.raises(
        PackagedBoundLocalProductFactoryHostError,
        match="changed during contained-local execution",
    ):
        await entry.program.ports.collect(request, object(), object())


def test_entry_repository_authority_rejects_same_root_rebind_version_change(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository_ref()
    project = _project(store, repository)
    root = _repository(tmp_path, "durable repository")
    bindings = ProductFactoryLocalRepositoryBindings(store)
    first = bindings.bind(
        project_id=project.project_id,
        repository=repository,
        root=root,
        expected_binding_version=None,
    )
    host = PackagedBoundLocalProductFactoryHost(
        store,
        settings=_settings(store),
        startup=_startup(tmp_path),
        bindings=bindings,
    )
    current = host._bindings_for_graph(
        project,
        _graph(project.project_id, repository),
    )
    entry = host._entry_for("host-task", project, current)
    authority = entry.program.ports.repository_authority
    assert authority is not None

    second = bindings.bind(
        project_id=project.project_id,
        repository=repository,
        root=root,
        expected_binding_version=first.binding_version,
    )
    assert second.root == first.root
    assert second.binding_version == first.binding_version + 1

    with pytest.raises(
        PackagedBoundLocalProductFactoryHostError,
        match="changed during contained-local execution",
    ):
        authority.require_component_root(
            project_id=project.project_id,
            repository_id=repository.repository_id,
            root=root,
        )


@pytest.mark.asyncio
async def test_entry_ports_fail_closed_if_binding_is_unbound_after_host_check(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository_ref()
    project = _project(store, repository)
    root = _repository(tmp_path, "durable repository")
    base_sha = _git(root, "rev-parse", "HEAD")
    bindings = ProductFactoryLocalRepositoryBindings(store)
    bound = bindings.bind(
        project_id=project.project_id,
        repository=repository,
        root=root,
        expected_binding_version=None,
    )
    host = PackagedBoundLocalProductFactoryHost(
        store,
        settings=_settings(store),
        startup=_startup(tmp_path),
        bindings=bindings,
    )
    state = host.initialize(
        host_task_id="host-task",
        project=project,
        graph=_graph(project.project_id, repository),
        graph_version=1,
        base_shas={repository.repository_id: base_sha},
        component_goals={"core": "Implement core"},
        permission_ceiling=frozenset({"read_source", "write_source", "run_tests"}),
    )
    entry = host._require_state_bindings("host-task", state)
    request = state.coordinator.snapshot().records[0].request

    bindings.unbind(
        project_id=project.project_id,
        repository_id=repository.repository_id,
        expected_binding_version=bound.binding_version,
    )

    with pytest.raises(
        PackagedBoundLocalProductFactoryHostError,
        match="binding is unavailable during contained-local execution",
    ):
        await entry.program.ports.context_for(request)


@pytest.mark.asyncio
async def test_entry_ports_fail_closed_if_repository_is_replaced_after_host_check(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository_ref()
    project = _project(store, repository)
    root = _repository(tmp_path, "durable repository")
    base_sha = _git(root, "rev-parse", "HEAD")
    bindings = ProductFactoryLocalRepositoryBindings(store)
    bindings.bind(
        project_id=project.project_id,
        repository=repository,
        root=root,
        expected_binding_version=None,
    )
    host = PackagedBoundLocalProductFactoryHost(
        store,
        settings=_settings(store),
        startup=_startup(tmp_path),
        bindings=bindings,
    )
    state = host.initialize(
        host_task_id="host-task",
        project=project,
        graph=_graph(project.project_id, repository),
        graph_version=1,
        base_shas={repository.repository_id: base_sha},
        component_goals={"core": "Implement core"},
        permission_ceiling=frozenset({"read_source", "write_source", "run_tests"}),
    )
    entry = host._require_state_bindings("host-task", state)
    request = state.coordinator.snapshot().records[0].request

    moved = tmp_path / "moved repository"
    root.rename(moved)
    root.mkdir()
    (root / ".git").mkdir()

    with pytest.raises(
        PackagedBoundLocalProductFactoryHostError,
        match="binding is unavailable during contained-local execution",
    ):
        await entry.program.ports.context_for(request)

@pytest.mark.asyncio
async def test_entry_ports_reject_product_project_version_change_after_host_check(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository_ref()
    project = _project(store, repository)
    root = _repository(tmp_path, "durable repository")
    base_sha = _git(root, "rev-parse", "HEAD")
    bindings = ProductFactoryLocalRepositoryBindings(store)
    bindings.bind(
        project_id=project.project_id,
        repository=repository,
        root=root,
        expected_binding_version=None,
    )
    host = PackagedBoundLocalProductFactoryHost(
        store,
        settings=_settings(store),
        startup=_startup(tmp_path),
        bindings=bindings,
    )
    state = host.initialize(
        host_task_id="host-task",
        project=project,
        graph=_graph(project.project_id, repository),
        graph_version=1,
        base_shas={repository.repository_id: base_sha},
        component_goals={"core": "Implement core"},
        permission_ceiling=frozenset(
            {"read_source", "write_source", "run_tests"}
        ),
    )
    entry = host._require_state_bindings("host-task", state)
    request = state.coordinator.snapshot().records[0].request

    changed = ProductProjectRepository(store).update_spec(
        project.project_id,
        ProductProjectSpec(
            goal="Changed after host-level repository validation",
            desired_outcome=project.spec.desired_outcome,
            repository_refs=project.spec.repository_refs,
        ),
        expected_row_version=project.row_version,
        change_reason="effect-boundary ProductProject race regression",
        idempotency_key="update:product-1:effect-boundary-race",
    )
    assert changed.row_version > project.row_version

    with pytest.raises(
        PackagedBoundLocalProductFactoryHostError,
        match="ProductProject changed during contained-local execution",
    ):
        await entry.program.ports.context_for(request)
    with pytest.raises(
        PackagedBoundLocalProductFactoryHostError,
        match="ProductProject changed during contained-local execution",
    ):
        await entry.program.ports.collect(request, object(), object())


@pytest.mark.asyncio
async def test_worker_revalidates_binding_after_context_before_execute(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository_ref()
    project = _project(store, repository)
    first_root = _repository(tmp_path, "first repository")
    second_root = _repository(tmp_path, "second repository")
    base_sha = _git(first_root, "rev-parse", "HEAD")
    bindings = ProductFactoryLocalRepositoryBindings(store)
    first = bindings.bind(
        project_id=project.project_id,
        repository=repository,
        root=first_root,
        expected_binding_version=None,
    )
    host = PackagedBoundLocalProductFactoryHost(
        store,
        settings=_settings(store),
        startup=_startup(tmp_path),
        bindings=bindings,
    )
    state = host.initialize(
        host_task_id="host-task",
        project=project,
        graph=_graph(project.project_id, repository),
        graph_version=1,
        base_shas={repository.repository_id: base_sha},
        component_goals={"core": "Implement core"},
        permission_ceiling=frozenset({"read_source", "write_source", "run_tests"}),
    )
    entry = host._require_state_bindings("host-task", state)
    request = state.coordinator.snapshot().records[0].request
    adapter = entry.program.host.worker
    original_contexts = adapter.contexts
    planner_calls: list[str] = []

    class RebindingContexts:
        async def context_for(self, active_request):
            context = await original_contexts.context_for(active_request)
            bindings.bind(
                project_id=project.project_id,
                repository=repository,
                root=second_root,
                expected_binding_version=first.binding_version,
            )
            return context

    class UnexpectedPlanner:
        async def plan(self, job):
            planner_calls.append(job.job_id)
            raise AssertionError("planner must not run after binding authority changes")

    adapter.contexts = RebindingContexts()
    entry.program.worker.planner = UnexpectedPlanner()

    with pytest.raises(
        PackagedBoundLocalProductFactoryHostError,
        match="changed during contained-local execution",
    ):
        await adapter.dispatch(request)

    assert planner_calls == []
    assert not (
        entry.program.worker.workspace_root_for(request.work_id) / "worktree"
    ).exists()


@pytest.mark.asyncio
async def test_worker_revalidates_binding_after_planner_await_before_effect(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository_ref()
    project = _project(store, repository)
    first_root = _repository(tmp_path, "first repository")
    second_root = _repository(tmp_path, "second repository")
    base_sha = _git(first_root, "rev-parse", "HEAD")
    bindings = ProductFactoryLocalRepositoryBindings(store)
    first = bindings.bind(
        project_id=project.project_id,
        repository=repository,
        root=first_root,
        expected_binding_version=None,
    )
    host = PackagedBoundLocalProductFactoryHost(
        store,
        settings=_settings(store),
        startup=_startup(tmp_path),
        bindings=bindings,
    )
    state = host.initialize(
        host_task_id="host-task",
        project=project,
        graph=_graph(project.project_id, repository),
        graph_version=1,
        base_shas={repository.repository_id: base_sha},
        component_goals={"core": "Implement core"},
        permission_ceiling=frozenset({"read_source", "write_source", "run_tests"}),
    )
    entry = host._require_state_bindings("host-task", state)
    request = state.coordinator.snapshot().records[0].request

    class RebindingPlanner:
        async def plan(self, job):
            bindings.bind(
                project_id=project.project_id,
                repository=repository,
                root=second_root,
                expected_binding_version=first.binding_version,
            )
            return LocalCodingPlan(
                (LocalFileEdit("src/new.py", b"print('candidate')\n"),)
            )

    entry.program.worker.planner = RebindingPlanner()

    with pytest.raises(
        PackagedBoundLocalProductFactoryHostError,
        match="changed during contained-local execution",
    ):
        await entry.program.host.worker.dispatch(request)

    assert not (
        entry.program.worker.workspace_root_for(request.work_id) / "worktree"
    ).exists()
