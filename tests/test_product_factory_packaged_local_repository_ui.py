from __future__ import annotations

import pathlib

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_local_repository_binding import (
    ProductFactoryLocalRepositoryBindings,
)
from nika_core.product_factory_orchestration import (
    ProductComponent,
    ProductRepositoryGraph,
    RepositoryRef,
)
from nika_core.product_factory_packaged_local_repository_ui import (
    PackagedLocalRepositoryBindingCommands,
)
from nika_core.product_factory_packaged_preparation import (
    PackagedProductFactoryExecutionPlan,
)
from nika_core.product_project import ProductProjectRepository, ProductProjectSpec


def _store(tmp_path: pathlib.Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "стан Ніки" / "nika.db")
    store.initialize()
    return store


def _repository(
    *,
    repository_id: str = "repo-1",
    locator: str = "Oleksii-debug/example",
) -> RepositoryRef:
    return RepositoryRef(
        repository_id=repository_id,
        provider="github",
        locator=locator,
        default_branch="main",
    )


def _project(
    store: SQLiteStore,
    repositories: tuple[RepositoryRef, ...],
):
    return ProductProjectRepository(store).create(
        project_id="product-1",
        name="Product 1",
        spec=ProductProjectSpec(
            goal="Build the accessible product",
            desired_outcome="Verified Windows package",
            repository_refs=tuple(repository.locator for repository in repositories),
        ),
        idempotency_key="create:product-1",
    )


def _plan(project, repository: RepositoryRef) -> PackagedProductFactoryExecutionPlan:
    return PackagedProductFactoryExecutionPlan(
        project_id=project.project_id,
        expected_spec_version=project.spec_version,
        expected_row_version=project.row_version,
        graph=ProductRepositoryGraph(
            project_id=project.project_id,
            repositories=(repository,),
            components=(
                ProductComponent(
                    component_id="core",
                    repository_id=repository.repository_id,
                    paths=("src",),
                ),
            ),
        ),
        graph_version=1,
        base_shas={repository.repository_id: "a" * 40},
        component_goals={"core": "Implement core"},
        permission_ceiling=frozenset(
            {"read_source", "write_source", "run_tests"}
        ),
    )


def _root(tmp_path: pathlib.Path, name: str) -> pathlib.Path:
    root = tmp_path / name
    root.mkdir()
    (root / ".git").mkdir()
    return root.resolve()


def _commands(
    bindings: ProductFactoryLocalRepositoryBindings,
    *,
    plan_box: dict[str, PackagedProductFactoryExecutionPlan],
    active_box: dict[str, str | None],
) -> PackagedLocalRepositoryBindingCommands:
    return PackagedLocalRepositoryBindingCommands(
        bindings,
        resolve_plan=lambda project_id: (
            plan_box["plan"]
            if plan_box["plan"].project_id == project_id
            else (_ for _ in ()).throw(KeyError(project_id))
        ),
        active_project_id=lambda: active_box["project_id"],
    )


def _bind_payload(
    snapshot: dict[str, object],
    root: pathlib.Path,
) -> dict[str, object]:
    repositories = snapshot["repositories"]
    assert isinstance(repositories, list)
    assert len(repositories) == 1
    repository = repositories[0]
    assert isinstance(repository, dict)
    return {
        "project_id": snapshot["project_id"],
        "repository_id": repository["repository_id"],
        "repository_token": repository["repository_token"],
        "root_path": str(root),
        "expected_binding_version": repository["binding_version"],
    }


def test_snapshot_requires_selected_project_and_loaded_plan(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository()
    project = _project(store, (repository,))
    plan_box = {"plan": _plan(project, repository)}
    active_box: dict[str, str | None] = {"project_id": None}
    commands = _commands(
        ProductFactoryLocalRepositoryBindings(store),
        plan_box=plan_box,
        active_box=active_box,
    )

    assert commands.snapshot()["status"] == "project_required"

    active_box["project_id"] = "another-product"
    snapshot = commands.snapshot()
    assert snapshot["status"] == "plan_required"
    assert snapshot["repositories"] == []


def test_bind_uses_loaded_plan_identity_without_projecting_local_path(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository()
    project = _project(store, (repository,))
    bindings = ProductFactoryLocalRepositoryBindings(store)
    plan_box = {"plan": _plan(project, repository)}
    active_box: dict[str, str | None] = {"project_id": project.project_id}
    commands = _commands(bindings, plan_box=plan_box, active_box=active_box)
    root = _root(tmp_path, "локальний репозиторій")

    before = commands.snapshot()
    result = commands.bind(_bind_payload(before, root))
    after = commands.snapshot()

    assert result.status == "completed"
    assert result.focus_id == "product-factory-local-repository-path"
    assert str(root) not in result.message
    assert str(root) not in str(after)
    repositories = after["repositories"]
    assert isinstance(repositories, list)
    assert repositories[0]["binding_status"] == "bound"
    assert repositories[0]["binding_version"] == 1
    bound = bindings.require(project.project_id, repository.repository_id)
    assert bound.root == root


def test_bind_rejects_stale_product_project_plan_without_mutation(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository()
    project = _project(store, (repository,))
    bindings = ProductFactoryLocalRepositoryBindings(store)
    plan_box = {"plan": _plan(project, repository)}
    active_box: dict[str, str | None] = {"project_id": project.project_id}
    commands = _commands(bindings, plan_box=plan_box, active_box=active_box)
    stale = commands.snapshot()
    updated = ProductProjectRepository(store).update_spec(
        project.project_id,
        ProductProjectSpec(
            goal="Changed while the loaded plan stayed stale",
            desired_outcome=project.spec.desired_outcome,
            repository_refs=project.spec.repository_refs,
        ),
        expected_row_version=project.row_version,
        change_reason="stale accessible binding plan regression",
        idempotency_key="update:product-1:stale-accessible-binding",
    )
    root = _root(tmp_path, "stale plan target")

    rejected = commands.bind(_bind_payload(stale, root))

    assert rejected.status == "rejected"
    assert rejected.focus_id == "product-factory-local-repository-path"
    assert "актуальність ProductProject" in rejected.message
    assert bindings.binding_version(project.project_id, repository.repository_id) is None

    plan_box["plan"] = _plan(updated, repository)
    current = commands.snapshot()
    completed = commands.bind(_bind_payload(current, root))
    assert completed.status == "completed"
    assert bindings.binding_version(project.project_id, repository.repository_id) == 1


def test_bind_rejects_stale_repository_identity_token(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    first = _repository(locator="Oleksii-debug/first")
    second = _repository(locator="Oleksii-debug/second")
    project = _project(store, (first, second))
    bindings = ProductFactoryLocalRepositoryBindings(store)
    plan_box = {"plan": _plan(project, first)}
    active_box: dict[str, str | None] = {"project_id": project.project_id}
    commands = _commands(bindings, plan_box=plan_box, active_box=active_box)
    stale = commands.snapshot()
    plan_box["plan"] = _plan(project, second)

    result = commands.bind(
        _bind_payload(stale, _root(tmp_path, "stale target"))
    )

    assert result.status == "rejected"
    assert "JSON-плані змінився" in result.message
    assert bindings.binding_version(project.project_id, first.repository_id) is None


def test_bind_rejects_stale_binding_version_without_overwrite(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository()
    project = _project(store, (repository,))
    bindings = ProductFactoryLocalRepositoryBindings(store)
    plan_box = {"plan": _plan(project, repository)}
    active_box: dict[str, str | None] = {"project_id": project.project_id}
    commands = _commands(bindings, plan_box=plan_box, active_box=active_box)
    first_root = _root(tmp_path, "first root")
    second_root = _root(tmp_path, "second root")
    third_root = _root(tmp_path, "third root")
    first = bindings.bind(
        project_id=project.project_id,
        repository=repository,
        root=first_root,
        expected_binding_version=None,
    )
    stale = commands.snapshot()
    second = bindings.bind(
        project_id=project.project_id,
        repository=repository,
        root=second_root,
        expected_binding_version=first.binding_version,
    )

    result = commands.bind(_bind_payload(stale, third_root))

    assert result.status == "rejected"
    assert "вже змінилася" in result.message
    current = bindings.require(project.project_id, repository.repository_id)
    assert current.binding_version == second.binding_version
    assert current.root == second_root


def test_invalid_root_snapshot_retains_version_for_explicit_repair(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository()
    project = _project(store, (repository,))
    bindings = ProductFactoryLocalRepositoryBindings(store)
    plan_box = {"plan": _plan(project, repository)}
    active_box: dict[str, str | None] = {"project_id": project.project_id}
    commands = _commands(bindings, plan_box=plan_box, active_box=active_box)
    first_root = _root(tmp_path, "missing root")
    bound = bindings.bind(
        project_id=project.project_id,
        repository=repository,
        root=first_root,
        expected_binding_version=None,
    )
    first_root.rename(tmp_path / "moved root")

    broken = commands.snapshot()
    repositories = broken["repositories"]
    assert isinstance(repositories, list)
    assert repositories[0]["binding_status"] == "invalid"
    assert repositories[0]["binding_version"] == bound.binding_version

    replacement = _root(tmp_path, "replacement root")
    result = commands.bind(_bind_payload(broken, replacement))

    assert result.status == "completed"
    repaired = bindings.require(project.project_id, repository.repository_id)
    assert repaired.binding_version == bound.binding_version + 1
    assert repaired.root == replacement


def test_bind_rejects_project_switch_and_relative_path(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository()
    project = _project(store, (repository,))
    bindings = ProductFactoryLocalRepositoryBindings(store)
    plan_box = {"plan": _plan(project, repository)}
    active_box: dict[str, str | None] = {"project_id": project.project_id}
    commands = _commands(bindings, plan_box=plan_box, active_box=active_box)
    snapshot = commands.snapshot()
    payload = _bind_payload(snapshot, _root(tmp_path, "unused root"))

    active_box["project_id"] = "product-switched"
    switched = commands.bind(payload)
    assert switched.status == "rejected"

    active_box["project_id"] = project.project_id
    payload["root_path"] = "relative/repository"
    relative = commands.bind(payload)
    assert relative.status == "rejected"
    assert bindings.binding_version(project.project_id, repository.repository_id) is None
