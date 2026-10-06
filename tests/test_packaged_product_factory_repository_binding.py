from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.default_actions import build_default_action_registry
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
from nika_core.product_factory_packaged_repository_binding import (
    PackagedProductFactoryRepositoryBindingController,
)
from nika_core.product_project import ProductProjectRepository, ProductProjectSpec

ROOT = Path(__file__).resolve().parents[1]


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "стан Ніки" / "nika.db")
    store.initialize()
    return store


def _repository() -> RepositoryRef:
    return RepositoryRef(
        repository_id="repo-core",
        provider="github",
        locator="Oleksii-debug/Nika-Core",
        default_branch="main",
    )


def _project(store: SQLiteStore, repository: RepositoryRef):
    return ProductProjectRepository(store).create(
        project_id="product-binding-ui",
        name="Binding UI",
        spec=ProductProjectSpec(
            goal="Finish the packaged product",
            desired_outcome="Verified Windows package",
            repository_refs=(repository.locator,),
        ),
        idempotency_key="create:binding-ui",
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
                    paths=("src/nika_core",),
                ),
            ),
        ),
        graph_version=1,
        base_shas={repository.repository_id: "a" * 40},
        component_goals={"core": "Finish packaged repository binding"},
        permission_ceiling=frozenset(
            {"read_source", "write_source", "run_tests"}
        ),
    )


def _root(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    (root / ".git").mkdir()
    return root.resolve()


def _controller(
    store: SQLiteStore,
    plan: PackagedProductFactoryExecutionPlan,
) -> PackagedProductFactoryRepositoryBindingController:
    projects = ProductProjectRepository(store)

    def resolve(project_id: str) -> PackagedProductFactoryExecutionPlan:
        if project_id != plan.project_id:
            raise ValueError("wrong project")
        return plan

    return PackagedProductFactoryRepositoryBindingController(
        bindings=ProductFactoryLocalRepositoryBindings(store, projects),
        projects=projects,
        resolve_plan=resolve,
    )


def test_controller_requires_selected_project_and_loaded_plan(tmp_path: Path) -> None:
    store = _store(tmp_path)
    repository = _repository()
    project = _project(store, repository)
    controller = _controller(store, _plan(project, repository))

    assert controller.snapshot(None)["status"] == "project_required"

    unavailable = PackagedProductFactoryRepositoryBindingController(
        bindings=ProductFactoryLocalRepositoryBindings(store),
        projects=ProductProjectRepository(store),
        resolve_plan=lambda _project_id: (_ for _ in ()).throw(
            ValueError("no plan loaded")
        ),
    )
    no_plan = unavailable.snapshot(project.project_id)
    assert no_plan["status"] == "plan_required"
    assert no_plan["repositories"] == []


def test_controller_binds_identity_only_from_exact_plan(tmp_path: Path) -> None:
    store = _store(tmp_path)
    repository = _repository()
    project = _project(store, repository)
    controller = _controller(store, _plan(project, repository))
    root = _root(tmp_path, "локальний репозиторій з пробілом")

    before = controller.snapshot(project.project_id)
    assert before["repositories"][0]["binding_status"] == "unbound"

    result = controller.bind(
        project.project_id,
        {
            "repository_id": repository.repository_id,
            "root": str(root),
            "expected_binding_version": None,
        },
    )

    assert result.status == "completed"
    assert result.focus_id == "product-factory-repository-root"
    row = controller.snapshot(project.project_id)["repositories"][0]
    assert row["binding_status"] == "bound"
    assert row["binding_version"] == 1
    assert row["root"] == str(root)
    assert "credential_ref" not in row


def test_controller_rebind_is_cas_fenced_and_can_repair_invalid_root(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository()
    project = _project(store, repository)
    controller = _controller(store, _plan(project, repository))
    first_root = _root(tmp_path, "first repository")
    second_root = _root(tmp_path, "second repository")

    assert controller.bind(
        project.project_id,
        {
            "repository_id": repository.repository_id,
            "root": str(first_root),
            "expected_binding_version": None,
        },
    ).status == "completed"

    stale = controller.bind(
        project.project_id,
        {
            "repository_id": repository.repository_id,
            "root": str(second_root),
            "expected_binding_version": 7,
        },
    )
    assert stale.status == "rejected"
    assert "змінилася" in stale.message

    first_root.rename(tmp_path / "first repository moved")
    invalid = controller.snapshot(project.project_id)["repositories"][0]
    assert invalid["binding_status"] == "invalid"
    assert invalid["binding_version"] == 1
    assert invalid["root"] is None

    repaired = controller.bind(
        project.project_id,
        {
            "repository_id": repository.repository_id,
            "root": str(second_root),
            "expected_binding_version": 1,
        },
    )
    assert repaired.status == "completed"
    repaired_row = controller.snapshot(project.project_id)["repositories"][0]
    assert repaired_row["binding_status"] == "bound"
    assert repaired_row["binding_version"] == 2


def test_bind_rejects_stale_plan_even_when_locator_survives(tmp_path: Path) -> None:
    store = _store(tmp_path)
    repository = _repository()
    project = _project(store, repository)
    plan = _plan(project, repository)
    projects = ProductProjectRepository(store)
    projects.update_spec(
        project.project_id,
        ProductProjectSpec(
            goal="Changed after plan admission",
            desired_outcome=project.spec.desired_outcome,
            repository_refs=project.spec.repository_refs,
        ),
        expected_row_version=project.row_version,
        change_reason="stale binding plan",
        idempotency_key="update:binding-ui:stale",
    )
    bindings = ProductFactoryLocalRepositoryBindings(store, projects)

    with pytest.raises(
        ProductFactoryLocalRepositoryBindingError,
        match="execution plan is stale",
    ):
        bindings.bind(
            project_id=project.project_id,
            repository=repository,
            root=_root(tmp_path, "stale root"),
            expected_binding_version=None,
            expected_project_spec_version=plan.expected_spec_version,
            expected_project_row_version=plan.expected_row_version,
        )

    controller = PackagedProductFactoryRepositoryBindingController(
        bindings=bindings,
        projects=projects,
        resolve_plan=lambda _project_id: plan,
    )
    assert controller.snapshot(project.project_id)["status"] == "stale_plan"


def test_controller_fails_closed_on_durable_read_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    repository = _repository()
    project = _project(store, repository)
    plan = _plan(project, repository)
    projects = ProductProjectRepository(store)
    bindings = ProductFactoryLocalRepositoryBindings(store, projects)
    controller = PackagedProductFactoryRepositoryBindingController(
        bindings=bindings,
        projects=projects,
        resolve_plan=lambda _project_id: plan,
    )

    def fail_project_read(_project_id: str):
        raise sqlite3.DatabaseError("private durable failure")

    monkeypatch.setattr(projects, "get", fail_project_read)
    unavailable = controller.snapshot(project.project_id)
    assert unavailable["status"] == "unavailable"
    assert unavailable["repositories"] == []
    assert "private durable failure" not in unavailable["message"]

    rejected = controller.bind(
        project.project_id,
        {
            "repository_id": repository.repository_id,
            "root": str(_root(tmp_path, "never bound")),
            "expected_binding_version": None,
        },
    )
    assert rejected.status == "failed"
    assert "private durable failure" not in rejected.message


def test_binding_snapshot_contains_operational_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    repository = _repository()
    project = _project(store, repository)
    plan = _plan(project, repository)
    projects = ProductProjectRepository(store)
    bindings = ProductFactoryLocalRepositoryBindings(store, projects)
    controller = PackagedProductFactoryRepositoryBindingController(
        bindings=bindings,
        projects=projects,
        resolve_plan=lambda _project_id: plan,
    )

    def fail_version_read(_project_id: str, _repository_id: str):
        raise sqlite3.DatabaseError("private binding failure")

    monkeypatch.setattr(bindings, "current_binding_version", fail_version_read)
    row = controller.snapshot(project.project_id)["repositories"][0]
    assert row["binding_status"] == "invalid"
    assert row["binding_version"] is None
    assert row["root"] is None


def test_controller_rejects_untrusted_repository_and_path_payload(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    repository = _repository()
    project = _project(store, repository)
    controller = _controller(store, _plan(project, repository))

    unknown = controller.bind(
        project.project_id,
        {
            "repository_id": "repo-injected",
            "root": str(_root(tmp_path, "injected")),
            "expected_binding_version": None,
        },
    )
    assert unknown.status == "rejected"
    assert unknown.focus_id == "product-factory-repository-id"

    relative = controller.bind(
        project.project_id,
        {
            "repository_id": repository.repository_id,
            "root": "relative repository",
            "expected_binding_version": None,
        },
    )
    assert relative.status == "rejected"

    injected = controller.bind(
        project.project_id,
        {
            "repository_id": repository.repository_id,
            "root": str(_root(tmp_path, "safe root")),
            "expected_binding_version": None,
            "provider": "caller-controlled",
        },
    )
    assert injected.status == "rejected"


def test_packaged_repository_binding_controls_and_bridge_contract() -> None:
    html = (ROOT / "src/nika_core/ui/web/index.html").read_text(encoding="utf-8")
    app = (ROOT / "src/nika_core/ui/web/app.js").read_text(encoding="utf-8")
    script = (ROOT / "scripts/nika_windows.py").read_text(encoding="utf-8")
    actions = {
        action.action_id: action
        for action in build_default_action_registry().all()
    }

    action = actions["product.factory.repository.bind"]
    assert action.label == "Зберегти локальний репозиторій Product Factory"
    assert action.category == "Product Factory"
    assert action.default_binding is None

    assert 'id="product-factory-repository-binding-heading"' in html
    assert '<label for="product-factory-repository-id">' in html
    assert 'id="product-factory-repository-id"' in html
    assert '<label for="product-factory-repository-root">' in html
    assert 'id="product-factory-repository-root"' in html
    assert 'maxlength="32767"' in html
    assert 'data-action-id="product.factory.repository.bind"' in html
    assert 'data-error-focus-target="product-factory-repository-root"' in html

    assert "renderProductFactoryRepositoryBindings(" in app
    assert '"unavailable",' in app
    assert "state.product_factory_repository_bindings ?? null" in app
    assert 'actionId === "product.factory.repository.bind"' in app
    assert "payload.repository_id = repositoryId;" in app
    assert 'payload.root = productFactoryRepositoryRoot?.value ?? "";' in app
    assert "payload.expected_binding_version" in app
    assert "productFactoryRepositoryEditVersion" in app
    assert (
        "nextSelectedRow.binding_version !== productFactoryRepositoryEditVersion"
        in app
    )
    assert "незбережений шлях скинуто" in app

    assert "ProductFactoryLocalRepositoryBindings(" in script
    assert "PackagedProductFactoryRepositoryBindingController(" in script
    assert (
        '"product.factory.repository.bind": bind_product_factory_repository'
        in script
    )
    assert 'state["product_factory_repository_bindings"] = (' in script
