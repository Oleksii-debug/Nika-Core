from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_command.product_project_adapter import ProductProjectCommandService
from nika_core.product_factory_packaged_journey import (
    PackagedProductCommandRouter,
    PackagedProductJourneyError,
    PackagedProductSelectionStore,
    product_project_identity,
)
from nika_core.product_project import ProductProjectRepository
from nika_core.ui.bridge_models import UIResult


class OrdinaryHandler:
    def __init__(self) -> None:
        self.calls: list[Mapping[str, Any]] = []

    def __call__(self, payload: Mapping[str, Any]) -> UIResult:
        self.calls.append(payload)
        return UIResult(
            request_id="desktop-handler",
            status="completed",
            message="ordinary-task",
            focus_id="tasks-heading",
        )


def _durable_router(
    path: Path,
) -> tuple[
    PackagedProductCommandRouter,
    ProductProjectRepository,
    PackagedProductSelectionStore,
    OrdinaryHandler,
]:
    store = SQLiteStore(path)
    store.initialize()
    repository = ProductProjectRepository(store)
    selection = PackagedProductSelectionStore(store)
    ordinary = OrdinaryHandler()
    router = PackagedProductCommandRouter(
        products=ProductProjectCommandService(repository),
        ordinary_handler=ordinary,
        selection_store=selection,
    )
    return router, repository, selection, ordinary


def test_current_productproject_reports_exact_durable_selection_after_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "current product journey.db"
    goal = "Створи застосунок для доступного обліку рахунків"
    project_id = product_project_identity(goal)

    first, first_repository, first_selection, first_ordinary = _durable_router(database)
    created = first.create({"command": goal})

    assert created.status == "completed"
    assert first_repository.get(project_id).spec.goal == goal
    assert first_selection.load() == project_id
    assert first_ordinary.calls == []

    restarted, restarted_repository, restarted_selection, restarted_ordinary = _durable_router(
        database
    )
    assert restarted.active_project_id == project_id

    current = restarted.create({"command": "Show current ProductProject"})

    assert current.status == "completed"
    assert current.focus_id == "tasks-heading"
    assert current.message == (
        f"Поточний ProductProject: {project_id}; "
        f"spec version 1; state active; goal: {goal}."
    )
    recovered = restarted_repository.get(project_id)
    assert recovered.spec.goal == goal
    assert recovered.spec_version == 1
    assert restarted_selection.load() == project_id
    assert restarted_ordinary.calls == []


@pytest.mark.parametrize(
    "command",
    [
        "Current ProductProject",
        "Show current ProductProject",
        "Поточний ProductProject",
        "Покажи поточний ProductProject",
        "  show   current   productproject:  ",
    ],
)
def test_current_productproject_keyboard_aliases_are_exact_and_non_mutating(
    tmp_path: Path,
    command: str,
) -> None:
    database = tmp_path / "current aliases.db"
    goal = "Create product application for accessible invoice review"
    project_id = product_project_identity(goal)
    router, repository, selection, ordinary = _durable_router(database)
    router.create({"command": goal})
    before = repository.get(project_id)

    result = router.create({"command": command})

    assert result.status == "completed"
    assert project_id in result.message
    assert "spec version 1" in result.message
    assert "state active" in result.message
    assert repository.get(project_id) == before
    assert selection.load() == project_id
    assert ordinary.calls == []


def test_current_productproject_without_selection_fails_closed(tmp_path: Path) -> None:
    router, _repository, selection, ordinary = _durable_router(tmp_path / "empty.db")

    with pytest.raises(PackagedProductJourneyError, match="не вибрано"):
        router.create({"command": "Current ProductProject"})

    assert router.active_project_id is None
    assert selection.load() is None
    assert ordinary.calls == []


def test_current_productproject_clears_stale_durable_selection(tmp_path: Path) -> None:
    database = tmp_path / "stale current.db"
    store = SQLiteStore(database)
    store.initialize()
    repository = ProductProjectRepository(store)
    selection = PackagedProductSelectionStore(store)
    missing_project_id = "product-" + "f" * 64
    selection.select(missing_project_id)
    ordinary = OrdinaryHandler()
    router = PackagedProductCommandRouter(
        products=ProductProjectCommandService(repository),
        ordinary_handler=ordinary,
        selection_store=selection,
    )

    assert router.active_project_id == missing_project_id
    with pytest.raises(PackagedProductJourneyError, match="Застарілий вибір очищено"):
        router.create({"command": "Покажи поточний ProductProject"})

    assert router.active_project_id is None
    assert selection.load() is None
    assert ordinary.calls == []
