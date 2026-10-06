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


def test_snapshot_fails_closed_if_bound_filesystem_identity_changes(
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
    root.mkdir()
    (root / ".git").mkdir()

    snapshot = operator.snapshot(project.project_id)

    assert snapshot["status"] == "invalid"
    assert snapshot["repositories"] == []


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


def test_operator_rejects_stale_plan_bind_without_durable_mutation(
    tmp_path: pathlib.Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository()
    project = _project(store, repository)
    stale_plan = _plan(project, repository)
    operator = _operator(store, stale_plan)
    updated = ProductProjectRepository(store).update_spec(
        project.project_id,
        ProductProjectSpec(
            goal="Changed while the loaded plan stayed stale",
            desired_outcome=project.spec.desired_outcome,
            repository_refs=project.spec.repository_refs,
        ),
        expected_row_version=project.row_version,
        change_reason="operator stale bind regression",
        idempotency_key="update:product-1:operator-stale-bind",
    )
    root = _root(tmp_path)

    snapshot = operator.snapshot(project.project_id)
    rejected = operator.bind(
        {
            "project_id": project.project_id,
            "repository_id": repository.repository_id,
            "root_path": str(root),
            "expected_binding_version": None,
        }
    )

    assert snapshot["status"] == "invalid"
    assert rejected.status == "rejected"
    with pytest.raises(KeyError):
        ProductFactoryLocalRepositoryBindings(store).require(
            project.project_id,
            repository.repository_id,
        )

    current = _operator(store, _plan(updated, repository))
    assert current.snapshot(project.project_id)["status"] == "ready"
    assert current.bind(
        {
            "project_id": project.project_id,
            "repository_id": repository.repository_id,
            "root_path": str(root),
            "expected_binding_version": None,
        }
    ).status == "completed"


def test_operator_rejects_stale_plan_unbind_without_revoking_binding(
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
    updated = ProductProjectRepository(store).update_spec(
        project.project_id,
        ProductProjectSpec(
            goal="Changed before stale operator unbind",
            desired_outcome=project.spec.desired_outcome,
            repository_refs=project.spec.repository_refs,
        ),
        expected_row_version=project.row_version,
        change_reason="operator stale unbind regression",
        idempotency_key="update:product-1:operator-stale-unbind",
    )

    rejected = operator.unbind(
        {
            "project_id": project.project_id,
            "repository_id": repository.repository_id,
            "expected_binding_version": 1,
        }
    )
    assert rejected.status == "rejected"
    assert (
        ProductFactoryLocalRepositoryBindings(store)
        .require(project.project_id, repository.repository_id)
        .binding_version
        == 1
    )

    current = _operator(store, _plan(updated, repository))
    assert current.unbind(
        {
            "project_id": project.project_id,
            "repository_id": repository.repository_id,
            "expected_binding_version": 1,
        }
    ).status == "completed"
