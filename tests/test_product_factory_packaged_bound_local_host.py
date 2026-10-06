from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

import nika_core.product_factory_packaged_bound_local_host as packaged_bound_local_host
from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
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
from nika_core.toolsmith.contracts import RecoveryState
from nika_core.toolsmith.local_worker import (
    ContainedLocalWorkerError,
    LocalCodingPlan,
    LocalFileEdit,
)
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
    project = ProductProjectRepository(store).create(
        project_id="product-1",
        name="Product 1",
        spec=ProductProjectSpec(
            goal="Build the product",
            desired_outcome="Verified package",
            repository_refs=(repository.locator,),
        ),
        idempotency_key="create:product-1",
    )
    TaskQueue(store).create_exact(
        task_id="host-task",
        workspace_id="test.product-factory",
        agent_id="product-factory",
        payload={
            "kind": "product_factory",
            "product_project_id": project.project_id,
        },
    )
    return project


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


@pytest.mark.parametrize("next_model", ("qwen3:8b", "qwen3:8b-next"))
def test_delayed_entry_rejects_any_model_revision_change_before_worker_build(
    tmp_path: pathlib.Path,
    next_model: str,
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
    settings = _settings(store)
    program = build_packaged_local_product_factory_program(
        store,
        settings=settings,
        startup=_startup(tmp_path),
    )
    changed = settings.configure(
        {
            "schema_version": 1,
            "revision": 1,
            "route_kind": "ollama",
            "provider_id": "ollama",
            "model": next_model,
            "base_url": "http://localhost:11434",
            "credential_ref": None,
            "private_data_allowed": False,
            "timeout_seconds": 60.0,
        }
    )
    assert changed.status == "completed"
    host = program.multi_repository_host
    resolved = host._bindings_for_graph(
        project,
        _graph(project.project_id, repository),
    )

    with pytest.raises(
        PackagedBoundLocalProductFactoryHostError,
        match="model authority changed after packaged startup",
    ):
        host._entry_for("delayed-host-task", project, resolved)


def test_admitted_entry_keeps_frozen_model_if_revision_changes_after_recheck(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
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
    settings = _settings(store)
    program = build_packaged_local_product_factory_program(
        store,
        settings=settings,
        startup=_startup(tmp_path),
    )
    host = program.multi_repository_host
    resolved = host._bindings_for_graph(
        project,
        _graph(project.project_id, repository),
    )
    original_build = getattr(
        packaged_bound_local_host,
        "_build_repository_bound_packaged_local_product_factory_program_with_authority",
    )
    observed: dict[str, object] = {}

    def mutate_after_admission_then_build(
        worker_store: SQLiteStore,
        *,
        settings: V01ModelSettings,
        startup: object,
        repositories: object,
        model_authority: object = None,
    ):
        changed = settings.configure(
            {
                "schema_version": 1,
                "revision": 1,
                "route_kind": "ollama",
                "provider_id": "ollama",
                "model": "qwen3:8b-raced",
                "base_url": "http://localhost:11434",
                "credential_ref": None,
                "private_data_allowed": False,
                "timeout_seconds": 60.0,
            }
        )
        assert changed.status == "completed"
        observed["model_authority"] = model_authority
        return original_build(
            worker_store,
            settings=settings,
            startup=startup,
            repositories=repositories,
            model_authority=model_authority,
        )

    monkeypatch.setattr(
        packaged_bound_local_host,
        "_build_repository_bound_packaged_local_product_factory_program_with_authority",
        mutate_after_admission_then_build,
    )

    entry = host._entry_for("admitted-host-task", project, resolved)
    frozen = observed["model_authority"]
    assert getattr(frozen, "revision") == 1
    assert len(getattr(frozen, "selection_sha256")) == 64
    assert getattr(frozen, "artifact_pin_sha256") is None
    assert getattr(frozen, "model") == "qwen3:8b"
    assert settings.snapshot()["revision"] == 2
    assert entry.program.worker.repositories
    assert host._entry_for("admitted-host-task", project, resolved) is entry

    with pytest.raises(
        PackagedBoundLocalProductFactoryHostError,
        match="model authority changed after packaged startup",
    ):
        host._entry_for("later-host-task", project, resolved)


def test_host_task_persists_secret_free_model_authority_snapshot(
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
    settings = _settings(store)
    host = PackagedBoundLocalProductFactoryHost(
        store,
        settings=settings,
        startup=_startup(tmp_path),
        bindings=bindings,
    )

    host.initialize(
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

    task = TaskQueue(store).get("host-task")
    persisted = task.payload[
        packaged_bound_local_host._MODEL_AUTHORITY_KEY
    ]
    assert persisted["schema"] == (
        "nika.product-factory.packaged-model-authority.v1"
    )
    assert persisted["revision"] == 1
    assert persisted["model"] == "qwen3:8b"
    assert persisted["base_url"] == "http://localhost:11434"
    assert len(persisted["selection_sha256"]) == 64
    assert persisted["artifact_pin_sha256"] is None
    assert "credential" not in json.dumps(persisted).casefold()


def test_restart_restores_original_model_authority_after_settings_change(
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
    settings = _settings(store)
    host = PackagedBoundLocalProductFactoryHost(
        store,
        settings=settings,
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
    admitted = host._require_state_bindings("host-task", state)
    assert admitted.model_authority.model == "qwen3:8b"
    assert admitted.program.worker.planner.model == "qwen3:8b"

    changed = settings.configure(
        {
            "schema_version": 1,
            "revision": 1,
            "route_kind": "ollama",
            "provider_id": "ollama",
            "model": "qwen3:8b-next",
            "base_url": "http://localhost:11434",
            "credential_ref": None,
            "private_data_allowed": False,
            "timeout_seconds": 60.0,
        }
    )
    assert changed.status == "completed"

    reopened_store = SQLiteStore(store.path)
    reopened_store.initialize()
    reopened_settings = V01ModelSettings(reopened_store)
    restarted = PackagedBoundLocalProductFactoryHost(
        reopened_store,
        settings=reopened_settings,
        startup=_startup(tmp_path),
    )
    reopened_project = ProductProjectRepository(reopened_store).get(
        project.project_id
    )
    restored = restarted.restore(
        host_task_id="host-task",
        project=reopened_project,
    )
    entry = restarted._require_state_bindings("host-task", restored)

    assert restarted._model_authority.model == "qwen3:8b-next"
    assert entry.model_authority.model == "qwen3:8b"
    assert entry.program.worker.planner.model == "qwen3:8b"


def test_restart_fails_closed_for_legacy_checkpoint_without_model_authority(
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
    settings = _settings(store)
    host = PackagedBoundLocalProductFactoryHost(
        store,
        settings=settings,
        startup=_startup(tmp_path),
        bindings=bindings,
    )
    host.initialize(
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

    with store.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT payload_json FROM tasks WHERE task_id = ?",
            ("host-task",),
        ).fetchone()
        payload = json.loads(row["payload_json"])
        payload.pop(packaged_bound_local_host._MODEL_AUTHORITY_KEY)
        conn.execute(
            "UPDATE tasks SET payload_json = ? WHERE task_id = ?",
            (
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "host-task",
            ),
        )

    reopened_store = SQLiteStore(store.path)
    reopened_store.initialize()
    restarted = PackagedBoundLocalProductFactoryHost(
        reopened_store,
        settings=V01ModelSettings(reopened_store),
        startup=_startup(tmp_path),
    )
    reopened_project = ProductProjectRepository(reopened_store).get(
        project.project_id
    )

    with pytest.raises(
        PackagedBoundLocalProductFactoryHostError,
        match="has no durable model authority",
    ):
        restarted.restore(
            host_task_id="host-task",
            project=reopened_project,
        )


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
        host._entry_for("host-task", project, changed)


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


@pytest.mark.asyncio
async def test_worker_revalidates_binding_at_sync_effect_entry(
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
    original_authority = entry.program.worker.repository_authority
    assert original_authority is not None

    class RebindingAfterPostPlannerAuthority:
        def __init__(self) -> None:
            self.calls = 0

        def require_repository_root(
            self,
            *,
            repository_id: str,
            root: pathlib.Path,
        ) -> None:
            self.calls += 1
            original_authority.require_repository_root(
                repository_id=repository_id,
                root=root,
            )
            if self.calls == 5:
                bindings.bind(
                    project_id=project.project_id,
                    repository=repository,
                    root=second_root,
                    expected_binding_version=first.binding_version,
                )

    class StaticPlanner:
        async def plan(self, job):
            return LocalCodingPlan(
                (LocalFileEdit("src/new.py", b"print('candidate')\n"),)
            )

    race_authority = RebindingAfterPostPlannerAuthority()
    entry.program.worker.repository_authority = race_authority
    entry.program.worker.planner = StaticPlanner()

    with pytest.raises(
        PackagedBoundLocalProductFactoryHostError,
        match="changed during contained-local execution",
    ):
        await entry.program.host.worker.dispatch(request)

    assert race_authority.calls == 6
    assert not (
        entry.program.worker.workspace_root_for(request.work_id) / "worktree"
    ).exists()


@pytest.mark.asyncio
async def test_worker_recovery_revalidates_binding_after_context_before_source_read(
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
    source_reads: list[str] = []

    class RebindingContexts:
        async def context_for(self, active_request):
            context = await original_contexts.context_for(active_request)
            bindings.bind(
                project_id=project.project_id,
                repository=repository,
                root=second_root,
                expected_binding_version=first.binding_version,
            )

            def unexpected_tree_digest(repository_id: str, pinned_sha: str) -> str:
                source_reads.append(f"{repository_id}:{pinned_sha}")
                raise AssertionError("stale repository must not be read during recovery")

            entry.program.worker.repository_tree_digest = unexpected_tree_digest
            return context

    adapter.contexts = RebindingContexts()

    with pytest.raises(
        PackagedBoundLocalProductFactoryHostError,
        match="changed during contained-local execution",
    ):
        await adapter.recover(
            request,
            RecoveryState("terminal", "candidate-not-needed"),
        )

    assert source_reads == []


@pytest.mark.asyncio
async def test_worker_recovery_revalidates_binding_after_durable_state_read(
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

    planned_jobs: list[object] = []

    class StaticPlanner:
        async def plan(self, job):
            planned_jobs.append(job)
            return LocalCodingPlan(
                (LocalFileEdit("src/new.py", b"print('candidate')\n"),)
            )

    entry.program.worker.planner = StaticPlanner()
    await entry.program.host.worker.dispatch(request)
    assert len(planned_jobs) == 1

    original_load_state = entry.program.worker._load_state
    rebound = False

    def load_state_then_rebind(job_id: str):
        nonlocal rebound
        durable = original_load_state(job_id)
        if durable is not None and not rebound:
            bindings.bind(
                project_id=project.project_id,
                repository=repository,
                root=second_root,
                expected_binding_version=first.binding_version,
            )
            rebound = True
        return durable

    entry.program.worker._load_state = load_state_then_rebind

    with pytest.raises(
        PackagedBoundLocalProductFactoryHostError,
        match="changed during contained-local execution",
    ):
        await entry.program.host.worker.recover(
            request,
            RecoveryState("terminal", "candidate-present"),
        )

    assert rebound is True

    entry.program.worker._load_state = original_load_state
    terminal_storage_reads: list[str] = []

    def unexpected_terminal_storage(evidence, result) -> None:
        terminal_storage_reads.append(evidence.result_sha)
        raise AssertionError("stale repository must not validate terminal storage")

    entry.program.worker._validate_terminal_storage = unexpected_terminal_storage

    replay = await entry.program.worker.execute(planned_jobs[0])
    assert replay.failure is not None
    assert replay.recovery_state is not None
    assert replay.recovery_state.phase == "manual_reconcile_required"

    with pytest.raises(
        ContainedLocalWorkerError,
        match="terminal execution evidence is invalid",
    ):
        entry.program.worker.execution_evidence(request.work_id)

    inspected = await entry.program.worker.inspect(request.work_id)
    assert inspected is not None
    assert inspected.phase == "manual_reconcile_required"
    assert terminal_storage_reads == []
