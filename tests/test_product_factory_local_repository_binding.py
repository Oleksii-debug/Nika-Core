from __future__ import annotations

import pathlib
import threading

import pytest

import nika_core.product_factory_local_repository_binding as binding_module

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
    repository_id: str = "repo-1",
    provider: str = "github",
    locator: str = "Oleksii-debug/example",
) -> RepositoryRef:
    return RepositoryRef(
        repository_id=repository_id,
        provider=provider,
        locator=locator,
        default_branch="main",
    )


def _create_project(
    store: SQLiteStore,
    repository: RepositoryRef,
):
    return _create_project_with_repositories(store, (repository,))


def _create_project_with_repositories(
    store: SQLiteStore,
    repositories: tuple[RepositoryRef, ...],
):
    projects = ProductProjectRepository(store)
    return projects.create(
        project_id="product-1",
        name="Product 1",
        spec=ProductProjectSpec(
            goal="Build the product",
            desired_outcome="Verified package",
            repository_refs=tuple(repository.locator for repository in repositories),
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
        generation_table = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' "
            "AND name='product_factory_local_repository_binding_generations'"
        ).fetchone()
        generation_columns = [
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(product_factory_local_repository_binding_generations)"
            )
        ]
        binding_columns = [
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(product_factory_local_repository_bindings)"
            )
        ]

    assert version == 8
    assert table is not None
    assert "PRIMARY KEY(project_id, repository_id)" in table[0]
    assert generation_table is not None
    assert "PRIMARY KEY(project_id, repository_id)" in generation_table[0]
    assert generation_columns == [
        "project_id",
        "repository_id",
        "last_binding_version",
    ]
    assert "git_target_device" in binding_columns
    assert "git_target_inode" in binding_columns
    assert "git_commondir_sha256" in binding_columns
    assert "git_common_device" in binding_columns
    assert "git_common_inode" in binding_columns


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


def test_binding_rejects_same_physical_root_for_different_repository_ids(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    first_repository = _repository_ref(
        repository_id="repo-1",
        locator="Oleksii-debug/first",
    )
    second_repository = _repository_ref(
        repository_id="repo-2",
        locator="Oleksii-debug/second",
    )
    project = _create_project_with_repositories(
        store,
        (first_repository, second_repository),
    )
    root = _root(tmp_path)
    bindings = ProductFactoryLocalRepositoryBindings(store)
    first = bindings.bind(
        project_id=project.project_id,
        repository=first_repository,
        root=root,
        expected_binding_version=None,
    )

    alias_path = root / ".." / root.name
    with pytest.raises(
        ProductFactoryLocalRepositoryBindingError,
        match="already bound to another repository identity",
    ):
        bindings.bind(
            project_id=project.project_id,
            repository=second_repository,
            root=alias_path,
            expected_binding_version=None,
        )

    assert bindings.require(
        project.project_id,
        first_repository.repository_id,
    ).binding_version == first.binding_version
    with pytest.raises(KeyError):
        bindings.require(project.project_id, second_repository.repository_id)


def test_binding_allows_distinct_roots_for_distinct_repository_ids(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    first_repository = _repository_ref(
        repository_id="repo-1",
        locator="Oleksii-debug/first",
    )
    second_repository = _repository_ref(
        repository_id="repo-2",
        locator="Oleksii-debug/second",
    )
    project = _create_project_with_repositories(
        store,
        (first_repository, second_repository),
    )
    first_root = _root(tmp_path, "first repository")
    second_root = _root(tmp_path, "second repository")
    bindings = ProductFactoryLocalRepositoryBindings(store)

    first = bindings.bind(
        project_id=project.project_id,
        repository=first_repository,
        root=first_root,
        expected_binding_version=None,
    )
    second = bindings.bind(
        project_id=project.project_id,
        repository=second_repository,
        root=second_root,
        expected_binding_version=None,
    )

    assert first.root == first_root.resolve(strict=True)
    assert second.root == second_root.resolve(strict=True)


def test_validate_plan_rejects_repository_outside_current_product_project(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository_ref()
    project = _create_project(store, repository)
    injected = _repository_ref(
        repository_id="repo-injected",
        locator="Oleksii-debug/injected",
    )
    bindings = ProductFactoryLocalRepositoryBindings(store)

    with pytest.raises(
        ProductFactoryLocalRepositoryBindingError,
        match="execution plan is stale",
    ):
        bindings.validate_plan(_plan(project, injected))


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


def test_bind_rejects_product_project_changed_before_write(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    repository = _repository_ref()
    project = _create_project(store, repository)
    root = _root(tmp_path)
    bindings = ProductFactoryLocalRepositoryBindings(store)
    validation_started = threading.Event()
    validation_release = threading.Event()
    original_filesystem_identity = binding_module._filesystem_identity

    def blocked_filesystem_identity(path: pathlib.Path):
        identity = original_filesystem_identity(path)
        if threading.current_thread().name == "binding-writer":
            validation_started.set()
            if not validation_release.wait(timeout=10):
                raise AssertionError("binding validation release timed out")
        return identity

    monkeypatch.setattr(
        binding_module,
        "_filesystem_identity",
        blocked_filesystem_identity,
    )
    errors: list[Exception] = []

    def bind_repository() -> None:
        try:
            bindings.bind(
                project_id=project.project_id,
                repository=repository,
                root=root,
                expected_binding_version=None,
            )
        except Exception as exc:
            errors.append(exc)

    writer = threading.Thread(target=bind_repository, name="binding-writer")
    writer.start()
    assert validation_started.wait(timeout=10)

    ProductProjectRepository(store).update_spec(
        project.project_id,
        ProductProjectSpec(
            goal="Changed while local root was being validated",
            desired_outcome=project.spec.desired_outcome,
            repository_refs=project.spec.repository_refs,
        ),
        expected_row_version=project.row_version,
        change_reason="concurrent binding regression",
        idempotency_key="update:product-1:binding-race",
    )
    validation_release.set()
    writer.join(timeout=10)
    assert not writer.is_alive()

    assert len(errors) == 1
    assert isinstance(errors[0], ProductFactoryLocalRepositoryBindingError)
    assert "ProductProject changed while binding" in str(errors[0])
    with pytest.raises(KeyError):
        bindings.require(project.project_id, repository.repository_id)


def test_gitfile_binding_tracks_target_directory_identity(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository_ref()
    project = _create_project(store, repository)
    root = tmp_path / "linked worktree"
    root.mkdir()
    target = tmp_path / "git metadata target"
    target.mkdir()
    (root / ".git").write_text(f"gitdir: {target}\n", encoding="utf-8")
    bindings = ProductFactoryLocalRepositoryBindings(store)

    bound = bindings.bind(
        project_id=project.project_id,
        repository=repository,
        root=root,
        expected_binding_version=None,
    )
    assert bindings.require(project.project_id, repository.repository_id) == bound

    moved = tmp_path / "original git metadata target"
    target.rename(moved)
    target.mkdir()

    with pytest.raises(
        ProductFactoryLocalRepositoryBindingError,
        match="filesystem identity changed",
    ):
        bindings.require(project.project_id, repository.repository_id)


def test_gitfile_binding_accepts_relative_gitdir_and_rejects_malformed_record(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository_ref()
    project = _create_project(store, repository)
    root = tmp_path / "relative worktree"
    root.mkdir()
    target = tmp_path / "relative metadata"
    target.mkdir()
    (root / ".git").write_text(
        "gitdir: ../relative metadata\n",
        encoding="utf-8",
    )
    bindings = ProductFactoryLocalRepositoryBindings(store)

    bound = bindings.bind(
        project_id=project.project_id,
        repository=repository,
        root=root,
        expected_binding_version=None,
    )
    assert bindings.require(project.project_id, repository.repository_id) == bound

    bindings.unbind(
        project_id=project.project_id,
        repository_id=repository.repository_id,
        expected_binding_version=bound.binding_version,
        expected_repository=repository,
    )
    (root / ".git").write_text("not-a-gitdir\n", encoding="utf-8")

    with pytest.raises(
        ProductFactoryLocalRepositoryBindingError,
        match="invalid gitdir record",
    ):
        bindings.bind(
            project_id=project.project_id,
            repository=repository,
            root=root,
            expected_binding_version=None,
        )


def test_gitfile_binding_tracks_common_directory_identity(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository_ref()
    project = _create_project(store, repository)
    root = tmp_path / "linked common worktree"
    root.mkdir()
    git_directory = tmp_path / "worktree gitdir"
    git_directory.mkdir()
    common = tmp_path / "common git metadata"
    common.mkdir()
    (root / ".git").write_text(f"gitdir: {git_directory}\n", encoding="utf-8")
    (git_directory / "commondir").write_text(
        "../common git metadata\n",
        encoding="utf-8",
    )
    bindings = ProductFactoryLocalRepositoryBindings(store)

    bound = bindings.bind(
        project_id=project.project_id,
        repository=repository,
        root=root,
        expected_binding_version=None,
    )
    assert bindings.require(project.project_id, repository.repository_id) == bound

    moved = tmp_path / "original common git metadata"
    common.rename(moved)
    common.mkdir()

    with pytest.raises(
        ProductFactoryLocalRepositoryBindingError,
        match="filesystem identity changed",
    ):
        bindings.require(project.project_id, repository.repository_id)


def test_gitfile_binding_rejects_malformed_commondir_record(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository_ref()
    project = _create_project(store, repository)
    root = tmp_path / "malformed common worktree"
    root.mkdir()
    git_directory = tmp_path / "malformed common gitdir"
    git_directory.mkdir()
    (root / ".git").write_text(f"gitdir: {git_directory}\n", encoding="utf-8")
    (git_directory / "commondir").write_text(
        "../common\nsecond-line\n",
        encoding="utf-8",
    )
    bindings = ProductFactoryLocalRepositoryBindings(store)

    with pytest.raises(
        ProductFactoryLocalRepositoryBindingError,
        match="commondir metadata has an invalid record",
    ):
        bindings.bind(
            project_id=project.project_id,
            repository=repository,
            root=root,
            expected_binding_version=None,
        )


def test_legacy_gitfile_binding_without_target_identity_fails_closed(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository_ref()
    project = _create_project(store, repository)
    root = tmp_path / "legacy worktree"
    root.mkdir()
    target = tmp_path / "legacy metadata"
    target.mkdir()
    (root / ".git").write_text(f"gitdir: {target}\n", encoding="utf-8")
    bindings = ProductFactoryLocalRepositoryBindings(store)
    bound = bindings.bind(
        project_id=project.project_id,
        repository=repository,
        root=root,
        expected_binding_version=None,
    )

    with store.connection() as conn:
        conn.execute(
            "UPDATE product_factory_local_repository_bindings "
            "SET git_target_device=NULL, git_target_inode=NULL "
            "WHERE project_id=? AND repository_id=?",
            (project.project_id, repository.repository_id),
        )

    assert bindings.current_binding_version(
        project.project_id,
        repository.repository_id,
    ) == bound.binding_version
    with pytest.raises(
        ProductFactoryLocalRepositoryBindingError,
        match="invalid persisted git_target_device",
    ):
        bindings.require(project.project_id, repository.repository_id)


def test_bind_rejects_filesystem_identity_changed_before_write(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    repository = _repository_ref()
    project = _create_project(store, repository)
    root = _root(tmp_path)
    moved = tmp_path / "repository moved before binding write"
    bindings = ProductFactoryLocalRepositoryBindings(store)
    original_require_project_repository = bindings._require_project_repository
    require_calls = 0

    def require_project_repository(project_id: str, locator: str):
        nonlocal require_calls
        current = original_require_project_repository(project_id, locator)
        require_calls += 1
        if require_calls == 2:
            root.rename(moved)
            root.mkdir()
            (root / ".git").mkdir()
        return current

    monkeypatch.setattr(
        bindings,
        "_require_project_repository",
        require_project_repository,
    )

    with pytest.raises(
        ProductFactoryLocalRepositoryBindingError,
        match="filesystem identity changed",
    ):
        bindings.bind(
            project_id=project.project_id,
            repository=repository,
            root=root,
            expected_binding_version=None,
        )

    assert bindings.current_binding_version(
        project.project_id,
        repository.repository_id,
    ) is None
    with store.connection() as conn:
        bound_audit_count = conn.execute(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE event_type='product_factory.local_repository.bound' "
            "AND entity_type='product_project' AND entity_id=?",
            (project.project_id,),
        ).fetchone()[0]
    assert bound_audit_count == 0


def test_rebind_rejects_filesystem_identity_changed_before_write_without_mutation(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    repository = _repository_ref()
    project = _create_project(store, repository)
    first_root = _root(tmp_path, "first repository")
    replacement_root = _root(tmp_path, "replacement repository")
    moved = tmp_path / "replacement moved before binding write"
    bindings = ProductFactoryLocalRepositoryBindings(store)
    first = bindings.bind(
        project_id=project.project_id,
        repository=repository,
        root=first_root,
        expected_binding_version=None,
    )
    original_require_project_repository = bindings._require_project_repository
    require_calls = 0

    def require_project_repository(project_id: str, locator: str):
        nonlocal require_calls
        current = original_require_project_repository(project_id, locator)
        require_calls += 1
        if require_calls == 2:
            replacement_root.rename(moved)
            replacement_root.mkdir()
            (replacement_root / ".git").mkdir()
        return current

    monkeypatch.setattr(
        bindings,
        "_require_project_repository",
        require_project_repository,
    )

    with pytest.raises(
        ProductFactoryLocalRepositoryBindingError,
        match="filesystem identity changed",
    ):
        bindings.bind(
            project_id=project.project_id,
            repository=repository,
            root=replacement_root,
            expected_binding_version=first.binding_version,
        )

    assert bindings.current_binding_version(
        project.project_id,
        repository.repository_id,
    ) == first.binding_version
    assert bindings.require(
        project.project_id,
        repository.repository_id,
    ).root == first_root.resolve(strict=True)
    with store.connection() as conn:
        bound_audit_count = conn.execute(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE event_type='product_factory.local_repository.bound' "
            "AND entity_type='product_project' AND entity_id=?",
            (project.project_id,),
        ).fetchone()[0]
    assert bound_audit_count == 1


def test_require_rejects_binding_changed_during_filesystem_validation(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
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
    validation_started = threading.Event()
    validation_release = threading.Event()
    original_require_identity = binding_module._require_filesystem_identity

    def blocked_require_identity(
        root: pathlib.Path,
        expected: object,
    ) -> None:
        original_require_identity(root, expected)
        if (
            threading.current_thread().name == "binding-reader"
            and root == first.root
        ):
            validation_started.set()
            if not validation_release.wait(timeout=10):
                raise AssertionError("reader validation release timed out")

    monkeypatch.setattr(
        binding_module,
        "_require_filesystem_identity",
        blocked_require_identity,
    )
    errors: list[Exception] = []

    def read_binding() -> None:
        try:
            bindings.require(project.project_id, repository.repository_id)
        except Exception as exc:
            errors.append(exc)

    reader = threading.Thread(target=read_binding, name="binding-reader")
    reader.start()
    assert validation_started.wait(timeout=10)

    second = bindings.bind(
        project_id=project.project_id,
        repository=repository,
        root=second_root,
        expected_binding_version=first.binding_version,
    )
    assert second.binding_version == first.binding_version + 1
    validation_release.set()
    reader.join(timeout=10)
    assert not reader.is_alive()

    assert len(errors) == 1
    assert isinstance(errors[0], ProductFactoryLocalRepositoryBindingError)
    assert "changed while resolving" in str(errors[0])


def test_require_rejects_relative_persisted_root_path(
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

    with store.connection() as conn:
        conn.execute(
            "UPDATE product_factory_local_repository_bindings "
            "SET root_path=? WHERE project_id=? AND repository_id=?",
            ("relative-repository", project.project_id, repository.repository_id),
        )

    with pytest.raises(
        ProductFactoryLocalRepositoryBindingError,
        match="invalid persisted root_path",
    ):
        bindings.require(project.project_id, repository.repository_id)


def test_expected_project_versions_reject_stale_plan_before_filesystem_work(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    repository = _repository_ref()
    project = _create_project(store, repository)
    plan = _plan(project, repository)
    ProductProjectRepository(store).update_spec(
        project.project_id,
        ProductProjectSpec(
            goal="Changed after plan admission",
            desired_outcome=project.spec.desired_outcome,
            repository_refs=project.spec.repository_refs,
        ),
        expected_row_version=project.row_version,
        change_reason="stale local repository binding plan",
        idempotency_key="update:product-1:stale-binding-plan",
    )
    bindings = ProductFactoryLocalRepositoryBindings(store)

    def filesystem_must_not_run(_root: pathlib.Path):
        raise AssertionError("filesystem validation ran for a stale execution plan")

    monkeypatch.setattr(
        binding_module,
        "_filesystem_identity",
        filesystem_must_not_run,
    )

    with pytest.raises(
        ProductFactoryLocalRepositoryBindingError,
        match="execution plan is stale",
    ):
        bindings.bind(
            project_id=project.project_id,
            repository=repository,
            root=tmp_path / "must-not-be-read",
            expected_binding_version=None,
            expected_project_spec_version=plan.expected_spec_version,
            expected_project_row_version=plan.expected_row_version,
        )

    assert bindings.current_binding_version(
        project.project_id,
        repository.repository_id,
    ) is None


def test_current_binding_version_survives_invalid_filesystem_identity(
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

    root.rename(tmp_path / "repository moved")

    assert bindings.current_binding_version(
        project.project_id,
        repository.repository_id,
    ) == bound.binding_version
    with pytest.raises(ProductFactoryLocalRepositoryBindingError):
        bindings.require(project.project_id, repository.repository_id)


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


def test_unbind_expected_repository_identity_rejects_substitution_without_mutation(
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
    substituted = _repository_ref(
        repository_id=repository.repository_id,
        provider="git",
        locator=repository.locator,
    )

    with pytest.raises(
        ProductFactoryLocalRepositoryBindingError,
        match="does not match execution-plan repository",
    ):
        bindings.unbind(
            project_id=project.project_id,
            repository_id=repository.repository_id,
            expected_binding_version=bound.binding_version,
            expected_repository=substituted,
        )

    assert bindings.current_binding_version(
        project.project_id,
        repository.repository_id,
    ) == bound.binding_version


def test_unbind_rechecks_expected_project_version_after_concurrent_change(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
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
    validation_started = threading.Event()
    validation_release = threading.Event()
    original = binding_module._require_expected_project_versions
    writer_calls = 0

    def blocked_project_version_check(
        current_project,
        *,
        expected_spec_version,
        expected_row_version,
    ) -> None:
        nonlocal writer_calls
        original(
            current_project,
            expected_spec_version=expected_spec_version,
            expected_row_version=expected_row_version,
        )
        if threading.current_thread().name == "unbind-writer":
            writer_calls += 1
            if writer_calls == 1:
                validation_started.set()
                if not validation_release.wait(timeout=10):
                    raise AssertionError("unbind validation release timed out")

    monkeypatch.setattr(
        binding_module,
        "_require_expected_project_versions",
        blocked_project_version_check,
    )
    errors: list[Exception] = []

    def unbind_repository() -> None:
        try:
            bindings.unbind(
                project_id=project.project_id,
                repository_id=repository.repository_id,
                expected_binding_version=bound.binding_version,
                expected_project_spec_version=project.spec_version,
                expected_project_row_version=project.row_version,
            )
        except Exception as exc:
            errors.append(exc)

    writer = threading.Thread(target=unbind_repository, name="unbind-writer")
    writer.start()
    assert validation_started.wait(timeout=10)

    ProductProjectRepository(store).update_spec(
        project.project_id,
        ProductProjectSpec(
            goal="Changed while unbind was waiting",
            desired_outcome=project.spec.desired_outcome,
            repository_refs=project.spec.repository_refs,
        ),
        expected_row_version=project.row_version,
        change_reason="concurrent unbind regression",
        idempotency_key="update:product-1:unbind-race",
    )
    validation_release.set()
    writer.join(timeout=10)
    assert not writer.is_alive()

    assert len(errors) == 1
    assert isinstance(errors[0], ProductFactoryLocalRepositoryBindingError)
    assert "execution plan is stale" in str(errors[0])
    assert bindings.current_binding_version(
        project.project_id,
        repository.repository_id,
    ) == bound.binding_version


def test_binding_generation_migration_backfills_live_version(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository_ref()
    project = _create_project(store, repository)
    bindings = ProductFactoryLocalRepositoryBindings(store)
    first = bindings.bind(
        project_id=project.project_id,
        repository=repository,
        root=_root(tmp_path, "first migration repository"),
        expected_binding_version=None,
    )
    second = bindings.bind(
        project_id=project.project_id,
        repository=repository,
        root=_root(tmp_path, "second migration repository"),
        expected_binding_version=first.binding_version,
    )

    with store.connection() as conn:
        conn.execute(
            "DROP TABLE product_factory_local_repository_binding_generations"
        )
        conn.execute(
            "DELETE FROM product_project_schema_migrations WHERE version=7"
        )

    restarted_store = SQLiteStore(tmp_path / "стан Ніки" / "nika.db")
    restarted_store.initialize()
    with restarted_store.connection() as conn:
        generation = conn.execute(
            "SELECT last_binding_version "
            "FROM product_factory_local_repository_binding_generations "
            "WHERE project_id=? AND repository_id=?",
            (project.project_id, repository.repository_id),
        ).fetchone()

    assert generation is not None
    assert generation["last_binding_version"] == second.binding_version


def test_unbind_rebind_advances_binding_generation_across_restart(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository_ref()
    project = _create_project(store, repository)
    bindings = ProductFactoryLocalRepositoryBindings(store)
    first = bindings.bind(
        project_id=project.project_id,
        repository=repository,
        root=_root(tmp_path, "first generation repository"),
        expected_binding_version=None,
    )
    bindings.unbind(
        project_id=project.project_id,
        repository_id=repository.repository_id,
        expected_binding_version=first.binding_version,
        expected_repository=repository,
    )
    assert bindings.current_binding_version(
        project.project_id,
        repository.repository_id,
    ) is None

    restarted_store = SQLiteStore(tmp_path / "стан Ніки" / "nika.db")
    restarted_store.initialize()
    restarted = ProductFactoryLocalRepositoryBindings(restarted_store)
    second = restarted.bind(
        project_id=project.project_id,
        repository=repository,
        root=_root(tmp_path, "second generation repository"),
        expected_binding_version=None,
    )

    assert second.binding_version == first.binding_version + 1
    with restarted_store.connection() as conn:
        generation = conn.execute(
            "SELECT last_binding_version "
            "FROM product_factory_local_repository_binding_generations "
            "WHERE project_id=? AND repository_id=?",
            (project.project_id, repository.repository_id),
        ).fetchone()
    assert generation is not None
    assert generation["last_binding_version"] == second.binding_version


def test_stale_pre_unbind_version_cannot_mutate_recreated_binding(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository_ref()
    project = _create_project(store, repository)
    bindings = ProductFactoryLocalRepositoryBindings(store)
    first = bindings.bind(
        project_id=project.project_id,
        repository=repository,
        root=_root(tmp_path, "stale first repository"),
        expected_binding_version=None,
    )
    bindings.unbind(
        project_id=project.project_id,
        repository_id=repository.repository_id,
        expected_binding_version=first.binding_version,
        expected_repository=repository,
    )
    replacement_root = _root(tmp_path, "stale replacement repository")
    replacement = bindings.bind(
        project_id=project.project_id,
        repository=repository,
        root=replacement_root,
        expected_binding_version=None,
    )
    assert replacement.binding_version == first.binding_version + 1

    with pytest.raises(
        ProductFactoryLocalRepositoryBindingError,
        match="version changed",
    ):
        bindings.unbind(
            project_id=project.project_id,
            repository_id=repository.repository_id,
            expected_binding_version=first.binding_version,
            expected_repository=repository,
        )
    with pytest.raises(
        ProductFactoryLocalRepositoryBindingError,
        match="version changed",
    ):
        bindings.bind(
            project_id=project.project_id,
            repository=repository,
            root=_root(tmp_path, "stale third repository"),
            expected_binding_version=first.binding_version,
        )

    current = bindings.require(
        project.project_id,
        repository.repository_id,
    )
    assert current.binding_version == replacement.binding_version
    assert current.root == replacement_root.resolve(strict=True)


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
