from __future__ import annotations

import pathlib

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
from nika_core.product_factory_packaged_preparation import (
    PackagedProductFactoryExecutionPlan,
)
from nika_core.product_project import ProductProjectRepository, ProductProjectSpec


def _store(tmp_path: pathlib.Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "стан Ніки" / "nika.db")
    store.initialize()
    return store


def _repository_ref(
    *,
    provider: str = "github",
    locator: str = "Oleksii-debug/example",
) -> RepositoryRef:
    return RepositoryRef(
        repository_id="repo-1",
        provider=provider,
        locator=locator,
        default_branch="main",
    )


def _create_project(
    store: SQLiteStore,
    repository: RepositoryRef,
):
    projects = ProductProjectRepository(store)
    return projects.create(
        project_id="product-1",
        name="Product 1",
        spec=ProductProjectSpec(
            goal="Build the product",
            desired_outcome="Verified package",
            repository_refs=(repository.locator,),
        ),
        idempotency_key="create:product-1",
    )


def _root(tmp_path: pathlib.Path, name: str = "локальний репозиторій") -> pathlib.Path:
    root = tmp_path / name
    root.mkdir()
    (root / ".git").mkdir()
    return root


def _plan(project, repository: RepositoryRef) -> PackagedProductFactoryExecutionPlan:
    graph = ProductRepositoryGraph(
        project_id=project.project_id,
        repositories=(repository,),
        components=(
            ProductComponent(
                component_id="core",
                repository_id=repository.repository_id,
                paths=("src",),
            ),
        ),
    )
    return PackagedProductFactoryExecutionPlan(
        project_id=project.project_id,
        expected_spec_version=project.spec_version,
        expected_row_version=project.row_version,
        graph=graph,
        graph_version=1,
        base_shas={repository.repository_id: "a" * 40},
        component_goals={"core": "Implement core"},
        permission_ceiling=frozenset({"read_source", "write_source", "run_tests"}),
    )


def test_schema_migration_adds_local_repository_binding_table(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)

    with store.connection() as conn:
        version = conn.execute(
            "SELECT MAX(version) FROM product_project_schema_migrations"
        ).fetchone()[0]
        table = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='product_factory_local_repository_bindings'"
        ).fetchone()

    assert version == 6
    assert table is not None
    assert "PRIMARY KEY(project_id, repository_id)" in table[0]


def test_binding_survives_restart_and_resolves_exact_plan(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository_ref()
    project = _create_project(store, repository)
    root = _root(tmp_path)
    bindings = ProductFactoryLocalRepositoryBindings(store)

    bound = bindings.bind(
        project_id=project.project_id,
        repository=repository,
        root=root,
        expected_binding_version=None,
    )

    assert bound.binding_version == 1
    fresh = ProductFactoryLocalRepositoryBindings(
        SQLiteStore(store.path),
    )
    resolved = fresh.resolve_for_plan(_plan(project, repository))
    assert resolved == {repository.repository_id: root.resolve(strict=True)}


def test_binding_update_requires_exact_version(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository_ref()
    project = _create_project(store, repository)
    first_root = _root(tmp_path, "first repository")
    second_root = _root(tmp_path, "second repository")
    bindings = ProductFactoryLocalRepositoryBindings(store)
    first = bindings.bind(
        project_id=project.project_id,
        repository=repository,
        root=first_root,
        expected_binding_version=None,
    )

    with pytest.raises(
        ProductFactoryLocalRepositoryBindingError,
        match="version changed",
    ):
        bindings.bind(
            project_id=project.project_id,
            repository=repository,
            root=second_root,
            expected_binding_version=first.binding_version + 1,
        )

    second = bindings.bind(
        project_id=project.project_id,
        repository=repository,
        root=second_root,
        expected_binding_version=first.binding_version,
    )
    assert second.binding_version == 2
    assert bindings.require(project.project_id, repository.repository_id).root == (
        second_root.resolve(strict=True)
    )


def test_binding_rejects_locator_outside_current_product_project(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository_ref()
    project = _create_project(store, repository)
    other = _repository_ref(locator="Oleksii-debug/other")
    bindings = ProductFactoryLocalRepositoryBindings(store)

    with pytest.raises(
        ProductFactoryLocalRepositoryBindingError,
        match="not present in current ProductProject",
    ):
        bindings.bind(
            project_id=project.project_id,
            repository=other,
            root=_root(tmp_path),
            expected_binding_version=None,
        )


def test_resolve_rejects_repository_identity_substitution(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository_ref()
    project = _create_project(store, repository)
    bindings = ProductFactoryLocalRepositoryBindings(store)
    bindings.bind(
        project_id=project.project_id,
        repository=repository,
        root=_root(tmp_path),
        expected_binding_version=None,
    )
    substituted = _repository_ref(provider="git", locator=repository.locator)

    with pytest.raises(
        ProductFactoryLocalRepositoryBindingError,
        match="does not match",
    ):
        bindings.resolve_for_plan(_plan(project, substituted))


def test_resolve_rejects_replaced_repository_root(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository_ref()
    project = _create_project(store, repository)
    root = _root(tmp_path)
    bindings = ProductFactoryLocalRepositoryBindings(store)
    bindings.bind(
        project_id=project.project_id,
        repository=repository,
        root=root,
        expected_binding_version=None,
    )
    moved = tmp_path / "original repository moved"
    root.rename(moved)
    root.mkdir()
    (root / ".git").mkdir()

    with pytest.raises(
        ProductFactoryLocalRepositoryBindingError,
        match="filesystem identity changed",
    ):
        bindings.resolve_for_plan(_plan(project, repository))


def test_binding_rejects_inline_repository_credentials(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    locator = "https://user:secret@example.test/org/repo.git"
    repository = _repository_ref(locator=locator)
    project = _create_project(store, repository)
    bindings = ProductFactoryLocalRepositoryBindings(store)

    with pytest.raises(
        ProductFactoryLocalRepositoryBindingError,
        match="credential",
    ):
        bindings.bind(
            project_id=project.project_id,
            repository=repository,
            root=_root(tmp_path),
            expected_binding_version=None,
        )

    with pytest.raises(KeyError):
        bindings.require(project.project_id, repository.repository_id)


def test_unbind_is_version_fenced_and_removes_authority(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository_ref()
    project = _create_project(store, repository)
    bindings = ProductFactoryLocalRepositoryBindings(store)
    bound = bindings.bind(
        project_id=project.project_id,
        repository=repository,
        root=_root(tmp_path),
        expected_binding_version=None,
    )

    with pytest.raises(
        ProductFactoryLocalRepositoryBindingError,
        match="version changed",
    ):
        bindings.unbind(
            project_id=project.project_id,
            repository_id=repository.repository_id,
            expected_binding_version=bound.binding_version + 1,
        )

    bindings.unbind(
        project_id=project.project_id,
        repository_id=repository.repository_id,
        expected_binding_version=bound.binding_version,
    )
    with pytest.raises(KeyError):
        bindings.require(project.project_id, repository.repository_id)
