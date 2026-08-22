from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_command.product_project_adapter import ProductProjectCommandService
from nika_core.product_factory_packaged_journey import (
    PackagedProductCommandRouter,
    PackagedProductJourneyError,
    PackagedProductSelectionStore,
    packaged_current_product_command,
    product_project_identity,
)
from nika_core.product_project import ProductProjectRepository
from nika_core.ui.bridge_models import UIResult


def _router(path: Path) -> tuple[PackagedProductCommandRouter, ProductProjectRepository]:
    store = SQLiteStore(path)
    store.initialize()
    repository = ProductProjectRepository(store)
    router = PackagedProductCommandRouter(
        products=ProductProjectCommandService(repository),
        ordinary_handler=lambda _payload: UIResult(
            request_id="ordinary",
            status="completed",
            message="ordinary",
            focus_id="tasks-heading",
        ),
        selection_store=PackagedProductSelectionStore(store),
    )
    return router, repository


@pytest.mark.parametrize(
    "command",
    [
        "Current ProductProject",
        "show current productproject",
        "Поточний ProductProject",
        "  ПОКАЖИ   ПОТОЧНИЙ PRODUCTPROJECT: ",
    ],
)
def test_current_command_parser_is_keyboard_friendly(command: str) -> None:
    assert packaged_current_product_command(command)


def test_current_command_does_not_intercept_ordinary_text() -> None:
    assert not packaged_current_product_command("Summarize the current ProductProject architecture")


def test_current_command_reports_durable_identity_after_restart(tmp_path: Path) -> None:
    database = tmp_path / "current project.db"
    router, _ = _router(database)
    goal = "Create product application for accessible warehouse labels"
    project_id = product_project_identity(goal)
    router.create({"command": goal})

    restarted, _ = _router(database)
    result = restarted.create({"command": "Покажи поточний ProductProject"})

    assert result.status == "completed"
    assert result.focus_id == "tasks-heading"
    assert project_id in result.message
    assert goal in result.message
    assert "spec version 1" in result.message
    assert restarted.active_project_id == project_id


def test_current_command_without_selection_fails_closed(tmp_path: Path) -> None:
    router, _ = _router(tmp_path / "none.db")

    with pytest.raises(PackagedProductJourneyError, match="не вибрано"):
        router.create({"command": "Current ProductProject"})

    assert router.active_project_id is None


def test_current_command_clears_stale_selection_and_reports_error(tmp_path: Path) -> None:
    database = tmp_path / "stale.db"
    store = SQLiteStore(database)
    store.initialize()
    selection = PackagedProductSelectionStore(store)
    missing = "product-" + "a" * 64
    selection.select(missing)
    repository = ProductProjectRepository(store)
    router = PackagedProductCommandRouter(
        products=ProductProjectCommandService(repository),
        ordinary_handler=lambda _payload: UIResult(
            request_id="ordinary",
            status="completed",
            message="ordinary",
            focus_id="tasks-heading",
        ),
        selection_store=selection,
    )

    with pytest.raises(PackagedProductJourneyError, match="Застарілий вибір очищено"):
        router.create({"command": "Show current ProductProject"})

    assert router.active_project_id is None
    restarted, _ = _router(database)
    assert restarted.active_project_id is None
