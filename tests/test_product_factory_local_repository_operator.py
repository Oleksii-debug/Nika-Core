from __future__ import annotations

import pathlib

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_factory_local_repository_operator import (
    PackagedLocalRepositoryOperator,
)
from nika_core.product_factory_local_repository_binding import (
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
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    return store


def _repository() -> RepositoryRef:
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
            goal="Build",
            desired_outcome="Verified",
            repository_refs=(repository.locator,),
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
        component_goals={"core": "Build core"},
        permission_ceiling=frozenset({"read_source", "write_source", "run_tests"}),
    )


def _root(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "локальний репозиторій"
    root.mkdir()
    (root / ".git").mkdir()
    return root.resolve()


def _operator(store: SQLiteStore, plan: PackagedProductFactoryExecutionPlan):
    return PackagedLocalRepositoryOperator(
        bindings=ProductFactoryLocalRepositoryBindings(store),
        resolve_plan=lambda project_id: plan
        if project_id == plan.project_id
        else (_ for _ in ()).throw(KeyError(project_id)),
    )


def test_snapshot_lists_only_plan_repositories_without_root_path(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository()
    project = _project(store, repository)
    operator = _operator(store, _plan(project, repository))

    snapshot = operator.snapshot(project.project_id)

    assert snapshot["status"] == "ready"
    assert snapshot["repositories"] == [
        {
            "repository_id": "repo-1",
            "provider": "github",
            "locator": "Oleksii-debug/example",
            "binding_status": "unbound",
            "bound": False,
            "binding_version": None,
        }
    ]
    assert "root" not in repr(snapshot).casefold()


def test_explicit_bind_and_version_fenced_unbind_round_trip(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository()
    project = _project(store, repository)
    plan = _plan(project, repository)
    operator = _operator(store, plan)
    root = _root(tmp_path)

    bound = operator.bind(
        {
            "project_id": project.project_id,
            "repository_id": repository.repository_id,
            "root_path": str(root),
            "expected_binding_version": None,
        }
    )

    assert bound.status == "completed"
    snapshot = operator.snapshot(project.project_id)
    item = snapshot["repositories"][0]
    assert item["binding_status"] == "bound"
    assert item["bound"] is True
    assert item["binding_version"] == 1
    assert str(root) not in repr(snapshot)
    assert str(root) not in bound.message

    stale = operator.unbind(
        {
            "project_id": project.project_id,
            "repository_id": repository.repository_id,
            "expected_binding_version": 2,
        }
    )
    assert stale.status == "rejected"
    assert operator.snapshot(project.project_id)["repositories"][0]["bound"] is True

    removed = operator.unbind(
        {
            "project_id": project.project_id,
            "repository_id": repository.repository_id,
            "expected_binding_version": 1,
        }
    )
    assert removed.status == "completed"
    assert operator.snapshot(project.project_id)["repositories"][0]["bound"] is False


def test_unbind_rejects_plan_repository_identity_substitution_without_mutation(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository()
    project = _project(store, repository)
    bindings = ProductFactoryLocalRepositoryBindings(store)
    plan_box = {"plan": _plan(project, repository)}
    operator = PackagedLocalRepositoryOperator(
        bindings=bindings,
        resolve_plan=lambda project_id: plan_box["plan"]
        if project_id == project.project_id
        else (_ for _ in ()).throw(KeyError(project_id)),
    )
    root = _root(tmp_path)
    assert operator.bind(
        {
            "project_id": project.project_id,
            "repository_id": repository.repository_id,
            "root_path": str(root),
            "expected_binding_version": None,
        }
    ).status == "completed"

    substituted = RepositoryRef(
        repository_id=repository.repository_id,
        provider="git",
        locator=repository.locator,
        default_branch=repository.default_branch,
    )
    plan_box["plan"] = _plan(project, substituted)

    result = operator.unbind(
        {
            "project_id": project.project_id,
            "repository_id": repository.repository_id,
            "expected_binding_version": 1,
        }
    )

    assert result.status == "rejected"
    assert bindings.current_binding_version(
        project.project_id,
        repository.repository_id,
    ) == 1


def test_unbind_rejects_stale_execution_plan_without_removing_binding(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository()
    project = _project(store, repository)
    plan = _plan(project, repository)
    operator = _operator(store, plan)
    root = _root(tmp_path)
    assert operator.bind(
        {
            "project_id": project.project_id,
            "repository_id": repository.repository_id,
            "root_path": str(root),
            "expected_binding_version": None,
        }
    ).status == "completed"

    ProductProjectRepository(store).update_spec(
        project.project_id,
        ProductProjectSpec(
            goal="Changed before stale unbind",
            desired_outcome=project.spec.desired_outcome,
            repository_refs=project.spec.repository_refs,
        ),
        expected_row_version=project.row_version,
        change_reason="stale unbind regression",
        idempotency_key="update:product-1:stale-unbind",
    )

    result = operator.unbind(
        {
            "project_id": project.project_id,
            "repository_id": repository.repository_id,
            "expected_binding_version": 1,
        }
    )

    assert result.status == "rejected"
    assert ProductFactoryLocalRepositoryBindings(store).current_binding_version(
        project.project_id,
        repository.repository_id,
    ) == 1


def test_binding_projection_survives_restart_without_exposing_root(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository()
    project = _project(store, repository)
    plan = _plan(project, repository)
    root = _root(tmp_path)
    operator = _operator(store, plan)

    result = operator.bind(
        {
            "project_id": project.project_id,
            "repository_id": repository.repository_id,
            "root_path": str(root),
            "expected_binding_version": None,
        }
    )
    assert result.status == "completed"

    reopened = SQLiteStore(store.path)
    reopened.initialize()
    restarted_operator = _operator(reopened, plan)
    snapshot = restarted_operator.snapshot(project.project_id)

    assert snapshot == {
        "status": "ready",
        "project_id": project.project_id,
        "repositories": [
            {
                "repository_id": repository.repository_id,
                "provider": repository.provider,
                "locator": repository.locator,
                "binding_status": "bound",
                "bound": True,
                "binding_version": 1,
            }
        ],
        "message": (
            "Виберіть репозиторій з поточного плану та явно вкажіть "
            "його локальний Git-корінь."
        ),
    }
    assert str(root) not in repr(snapshot)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "project_id": "product-1",
            "repository_id": "other",
            "root_path": "C:\\repo",
            "expected_binding_version": None,
        },
        {
            "project_id": "product-1",
            "repository_id": "repo-1",
            "root_path": "relative",
            "expected_binding_version": None,
        },
        {
            "project_id": "product-1",
            "repository_id": "repo-1",
            "root_path": "C:\\repo",
            "expected_binding_version": 0,
        },
    ],
)
def test_bind_rejects_untrusted_identity_path_or_version(
    tmp_path: pathlib.Path,
    payload: dict[str, object],
) -> None:
    store = _store(tmp_path)
    repository = _repository()
    project = _project(store, repository)
    operator = _operator(store, _plan(project, repository))

    result = operator.bind(payload)

    assert result.status == "rejected"


def test_bind_rejects_payload_fields_that_try_to_supply_repository_authority(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository()
    project = _project(store, repository)
    operator = _operator(store, _plan(project, repository))
    root = _root(tmp_path)

    result = operator.bind(
        {
            "project_id": project.project_id,
            "repository_id": repository.repository_id,
            "root_path": str(root),
            "expected_binding_version": None,
            "provider": "other",
        }
    )

    assert result.status == "rejected"
    assert operator.snapshot(project.project_id)["repositories"][0]["bound"] is False


def test_invalid_filesystem_binding_keeps_redacted_cas_and_can_be_repaired(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository()
    project = _project(store, repository)
    operator = _operator(store, _plan(project, repository))
    root = _root(tmp_path)
    assert operator.bind(
        {
            "project_id": project.project_id,
            "repository_id": repository.repository_id,
            "root_path": str(root),
            "expected_binding_version": None,
        }
    ).status == "completed"

    moved = tmp_path / "moved"
    root.rename(moved)
    replacement = tmp_path / "replacement repository"
    replacement.mkdir()
    (replacement / ".git").mkdir()

    snapshot = operator.snapshot(project.project_id)

    assert snapshot["status"] == "ready"
    item = snapshot["repositories"][0]
    assert item["binding_status"] == "invalid"
    assert item["bound"] is False
    assert item["binding_version"] == 1
    assert str(root) not in repr(snapshot)
    assert str(moved) not in repr(snapshot)

    repaired = operator.bind(
        {
            "project_id": project.project_id,
            "repository_id": repository.repository_id,
            "root_path": str(replacement.resolve()),
            "expected_binding_version": 1,
        }
    )
    assert repaired.status == "completed"
    repaired_item = operator.snapshot(project.project_id)["repositories"][0]
    assert repaired_item["binding_status"] == "bound"
    assert repaired_item["bound"] is True
    assert repaired_item["binding_version"] == 2


def test_bind_rejects_stale_plan_even_when_repository_locator_survives(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository()
    project = _project(store, repository)
    plan = _plan(project, repository)
    operator = _operator(store, plan)
    ProductProjectRepository(store).update_spec(
        project.project_id,
        ProductProjectSpec(
            goal="Changed after execution-plan admission",
            desired_outcome=project.spec.desired_outcome,
            repository_refs=project.spec.repository_refs,
        ),
        expected_row_version=project.row_version,
        change_reason="stale packaged repository plan",
        idempotency_key="update:product-1:stale-plan",
    )
    replacement = tmp_path / "stale plan root"
    replacement.mkdir()
    (replacement / ".git").mkdir()

    result = operator.bind(
        {
            "project_id": project.project_id,
            "repository_id": repository.repository_id,
            "root_path": str(replacement.resolve()),
            "expected_binding_version": None,
        }
    )

    assert result.status == "rejected"
    assert operator.snapshot(project.project_id)["status"] == "invalid"
    assert ProductFactoryLocalRepositoryBindings(store).current_binding_version(
        project.project_id,
        repository.repository_id,
    ) is None


def test_missing_plan_snapshot_and_actions_fail_closed(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    operator = PackagedLocalRepositoryOperator(
        bindings=ProductFactoryLocalRepositoryBindings(store),
        resolve_plan=lambda project_id: (_ for _ in ()).throw(KeyError(project_id)),
    )

    snapshot = operator.snapshot(None)
    assert snapshot["status"] == "missing_plan"
    result = operator.bind(
        {
            "project_id": "product-1",
            "repository_id": "repo-1",
            "root_path": str(tmp_path.resolve()),
            "expected_binding_version": None,
        }
    )
    assert result.status == "rejected"
