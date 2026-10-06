from __future__ import annotations

from pathlib import Path

from nika_core.config import AppConfig
from nika_core.data.sqlite import SQLiteStore
from nika_core.product_command.command_center import ProductCommandCenter
from nika_core.product_command.product_project_adapter import ProductProjectCommandService
from nika_core.product_factory_packaged_journey import (
    PackagedProductCommandRouter,
    PackagedProductSelectionStore,
    PackagedProductStateProvider,
    product_project_identity,
)
from nika_core.product_project import ProductProjectRepository
from nika_core.ui.bridge_models import UIResult
from scripts.nika_windows import build_windows_bridge


def _ordinary(_payload) -> UIResult:
    return UIResult(
        request_id="ordinary",
        status="completed",
        message="ordinary",
        focus_id="tasks-heading",
    )


def _stack(path: Path):
    store = SQLiteStore(path)
    store.initialize()
    repository = ProductProjectRepository(store)
    products = ProductProjectCommandService(repository)
    router = PackagedProductCommandRouter(
        products=products,
        ordinary_handler=_ordinary,
        selection_store=PackagedProductSelectionStore(store),
    )
    provider = PackagedProductStateProvider(
        base_state=lambda: {"tasks": [], "agents": [], "workspaces": []},
        router=router,
        command_center=ProductCommandCenter(products),
    )
    return store, repository, router, provider


def test_selected_product_project_is_visible_immediately_after_process_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "restart visible product.db"
    command = "Створи застосунок для доступного обліку витрат"
    project_id = product_project_identity(command)

    _store, repository, router, provider = _stack(database)
    router.create({"command": command})
    before = provider()["product_project"]
    assert before["project_id"] == project_id
    assert repository.get(project_id).spec_version == 1

    _restarted_store, restarted_repository, restarted_router, restarted_provider = _stack(
        database
    )
    recovered = restarted_provider()["product_project"]

    assert restarted_router.active_project_id == project_id
    assert recovered == before
    assert restarted_repository.get(project_id).spec_version == 1


def test_ordinary_command_does_not_erase_last_product_selection(tmp_path: Path) -> None:
    database = tmp_path / "ordinary preserves selection.db"
    product_command = "Create product application for accessible inventory"
    project_id = product_project_identity(product_command)

    _store, _repository, router, provider = _stack(database)
    router.create({"command": product_command})
    result = router.create({"command": "Count words in this text"})

    assert result.message == "ordinary"
    assert router.active_project_id == project_id
    assert provider()["product_project"]["project_id"] == project_id

    _store2, _repository2, restarted_router, restarted_provider = _stack(database)
    assert restarted_router.active_project_id == project_id
    assert restarted_provider()["product_project"]["project_id"] == project_id


def test_stale_selection_fails_safe_and_is_cleared(tmp_path: Path) -> None:
    database = tmp_path / "stale selection.db"
    store, _repository, router, provider = _stack(database)
    selection = PackagedProductSelectionStore(store)
    selection.select("product-missing")

    _store2, _repository2, restarted_router, restarted_provider = _stack(database)
    assert restarted_router.active_project_id == "product-missing"
    assert restarted_provider()["product_project"] is None
    assert restarted_router.active_project_id is None

    _store3, _repository3, second_restart_router, second_restart_provider = _stack(database)
    assert second_restart_router.active_project_id is None
    assert second_restart_provider()["product_project"] is None
    assert router.active_project_id is None
    assert provider()["product_project"] is None


def test_real_windows_bridge_restores_visible_project_without_replaying_command(
    tmp_path: Path,
) -> None:
    database = tmp_path / "windows composition restart.db"
    config = AppConfig(database_path=database)
    command = "Створи застосунок для доступного каталогу документів"
    project_id = product_project_identity(command)

    bridge, products = build_windows_bridge(config)
    result = bridge.dispatch(
        {
            "request_id": "first-command",
            "action_id": "task.create",
            "payload": {"command": command},
        }
    )
    assert result["status"] == "completed"
    assert products.inspect_project(project_id).summary.version == 1

    restarted_bridge, restarted_products = build_windows_bridge(config)
    recovered = restarted_bridge.get_state()["state"]["product_project"]

    assert recovered["project_id"] == project_id
    assert recovered["spec_version"] == 1
    assert restarted_products.inspect_project(project_id).summary.version == 1
