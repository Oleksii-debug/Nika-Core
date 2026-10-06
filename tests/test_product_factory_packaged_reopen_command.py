from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_command.product_project_adapter import ProductProjectCommandService
from nika_core.product_factory_packaged_journey import (
    PackagedProductCommandRouter,
    PackagedProductJourneyError,
    PackagedProductSelectionStore,
    packaged_product_reopen_target,
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


def test_explicit_reopen_switches_existing_project_and_survives_restart(tmp_path: Path) -> None:
    database = tmp_path / "product reopen.db"
    router, repository = _router(database)
    first_goal = "Create product application for accessible invoices"
    second_goal = "Create product application for accessible inventory"
    first_id = product_project_identity(first_goal)
    second_id = product_project_identity(second_goal)

    router.create({"command": first_goal})
    router.create({"command": second_goal})
    assert router.active_project_id == second_id

    result = router.create({"command": f"Відкрий ProductProject {first_id.upper()}"})

    assert result.status == "completed"
    assert first_id in result.message
    assert router.active_project_id == first_id
    assert repository.get(first_id).spec.goal == first_goal
    assert repository.get(second_id).spec.goal == second_goal

    restarted, _ = _router(database)
    assert restarted.active_project_id == first_id


def test_missing_reopen_target_fails_closed_without_changing_selection(tmp_path: Path) -> None:
    router, _ = _router(tmp_path / "missing.db")
    goal = "Create product application for deterministic notes"
    selected = product_project_identity(goal)
    router.create({"command": goal})
    missing = "product-" + "f" * 64
    if missing == selected:
        missing = "product-" + "e" * 64

    with pytest.raises(PackagedProductJourneyError, match="не знайдено"):
        router.create({"command": f"Open ProductProject {missing}"})

    assert router.active_project_id == selected
    restarted, _ = _router(tmp_path / "missing.db")
    assert restarted.active_project_id == selected


@pytest.mark.parametrize(
    "command",
    [
        "Open ProductProject",
        "Open ProductProject product-1234",
        "Відкрий ProductProject product-" + "g" * 64,
        "Reopen ProductProject product-" + "a" * 63,
    ],
)
def test_malformed_explicit_reopen_command_fails_closed(command: str) -> None:
    with pytest.raises(PackagedProductJourneyError, match="64 hex"):
        packaged_product_reopen_target(command)


def test_project_id_in_ordinary_text_is_not_intercepted() -> None:
    project_id = "product-" + "a" * 64
    assert packaged_product_reopen_target(f"Summarize this identifier {project_id}") is None


def test_reopen_parser_accepts_keyboard_friendly_spacing_and_case() -> None:
    project_id = "product-" + "a" * 64
    assert packaged_product_reopen_target(
        f"  OPEN   PRODUCTPROJECT:  {project_id.upper()}  "
    ) == project_id
