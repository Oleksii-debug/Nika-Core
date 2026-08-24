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
from nika_core.product_factory_packaged_refinement import (
    PackagedProductRefinementRouter,
    packaged_product_goal_refinement,
)
from nika_core.product_project import ProductProjectRepository, StaleProjectVersionError
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


def _durable_refinement_router(
    path: Path,
) -> tuple[
    PackagedProductRefinementRouter,
    PackagedProductCommandRouter,
    ProductProjectCommandService,
    ProductProjectRepository,
    PackagedProductSelectionStore,
    OrdinaryHandler,
]:
    store = SQLiteStore(path)
    store.initialize()
    repository = ProductProjectRepository(store)
    products = ProductProjectCommandService(repository)
    selection = PackagedProductSelectionStore(store)
    ordinary = OrdinaryHandler()
    base = PackagedProductCommandRouter(
        products=products,
        ordinary_handler=ordinary,
        selection_store=selection,
    )
    refinement = PackagedProductRefinementRouter(products=products, base_router=base)
    return refinement, base, products, repository, selection, ordinary


def test_refine_current_productproject_persists_new_version_and_survives_restart(
    tmp_path: Path,
) -> None:
    database = tmp_path / "refined product journey.db"
    original_goal = "Create accessible product application for invoice review"
    refined_goal = "Create accessible Windows product application for invoice review"
    project_id = product_project_identity(original_goal)
    first, base, _products, repository, selection, ordinary = _durable_refinement_router(
        database
    )

    created = first.create({"command": original_goal})
    current_before = first.create({"command": "Show current ProductProject"})
    refined = first.create(
        {"command": f"Set current ProductProject goal: {refined_goal}"}
    )

    assert created.status == "completed"
    assert current_before.message == (
        f"Поточний ProductProject: {project_id}; "
        f"spec version 1; state active; goal: {original_goal}."
    )
    assert refined.status == "completed"
    assert refined.focus_id == "tasks-heading"
    assert refined.message == (
        f"ProductProject оновлено: {project_id}; "
        f"spec version 1 -> 2; state active; goal: {refined_goal}."
    )
    durable = repository.get(project_id)
    assert durable.project_id == project_id
    assert durable.spec_version == 2
    assert durable.spec.goal == refined_goal
    assert selection.load() == project_id
    assert base.active_project_id == project_id
    assert ordinary.calls == []

    restarted, restarted_base, _service, restarted_repository, restarted_selection, restarted_ordinary = (
        _durable_refinement_router(database)
    )
    assert restarted_base.active_project_id == project_id

    recovered = restarted.create({"command": "Current ProductProject"})

    assert recovered.message == (
        f"Поточний ProductProject: {project_id}; "
        f"spec version 2; state active; goal: {refined_goal}."
    )
    assert restarted_repository.get(project_id).spec.goal == refined_goal
    assert restarted_repository.get(project_id).spec_version == 2
    assert restarted_selection.load() == project_id
    assert restarted_ordinary.calls == []


def test_refine_same_goal_is_idempotent_at_packaged_boundary(tmp_path: Path) -> None:
    database = tmp_path / "idempotent refinement.db"
    goal = "Create accessible product application for expense review"
    project_id = product_project_identity(goal)
    router, _base, _products, repository, _selection, _ordinary = (
        _durable_refinement_router(database)
    )
    router.create({"command": goal})

    result = router.create({"command": f"Update current ProductProject goal: {goal}"})

    assert result.message == (
        f"Ціль ProductProject вже актуальна: {project_id}; "
        f"spec version 1; state active; goal: {goal}."
    )
    assert repository.get(project_id).spec_version == 1


@pytest.mark.parametrize(
    ("command", "goal"),
    [
        (
            "Set current ProductProject goal: Build accessible Windows application",
            "Build accessible Windows application",
        ),
        (
            "Update current ProductProject goal: Build accessible service",
            "Build accessible service",
        ),
        (
            "Встанови ціль поточного ProductProject: Створи доступний застосунок",
            "Створи доступний застосунок",
        ),
        (
            "Онови ціль поточного ProductProject:   Створи   доступний   сервіс",
            "Створи доступний сервіс",
        ),
    ],
)
def test_refinement_keyboard_grammar_is_explicit(command: str, goal: str) -> None:
    assert packaged_product_goal_refinement(command) == goal


def test_refinement_does_not_intercept_ordinary_command(tmp_path: Path) -> None:
    router, _base, _products, _repository, _selection, ordinary = _durable_refinement_router(
        tmp_path / "ordinary.db"
    )

    result = router.create({"command": "Summarize my notes"})

    assert result.message == "ordinary-task"
    assert [call["command"] for call in ordinary.calls] == ["Summarize my notes"]


def test_refinement_requires_current_selection(tmp_path: Path) -> None:
    router, _base, _products, _repository, selection, ordinary = _durable_refinement_router(
        tmp_path / "no selection.db"
    )

    with pytest.raises(PackagedProductJourneyError, match="не вибрано"):
        router.create({"command": "Set current ProductProject goal: New product goal"})

    assert selection.load() is None
    assert ordinary.calls == []


def test_refinement_clears_stale_selection(tmp_path: Path) -> None:
    database = tmp_path / "stale refinement.db"
    store = SQLiteStore(database)
    store.initialize()
    repository = ProductProjectRepository(store)
    products = ProductProjectCommandService(repository)
    selection = PackagedProductSelectionStore(store)
    missing_project_id = "product-" + "e" * 64
    selection.select(missing_project_id)
    ordinary = OrdinaryHandler()
    base = PackagedProductCommandRouter(
        products=products,
        ordinary_handler=ordinary,
        selection_store=selection,
    )
    router = PackagedProductRefinementRouter(products=products, base_router=base)

    with pytest.raises(PackagedProductJourneyError, match="Застарілий вибір очищено"):
        router.create({"command": "Set current ProductProject goal: New product goal"})

    assert base.active_project_id is None
    assert selection.load() is None
    assert ordinary.calls == []


def test_refinement_normalizes_optimistic_concurrency_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    goal = "Create accessible product application for review"
    router, _base, products, repository, _selection, ordinary = _durable_refinement_router(
        tmp_path / "stale version.db"
    )
    router.create({"command": goal})

    def stale_update(*_args: Any, **_kwargs: Any) -> None:
        raise StaleProjectVersionError("controlled concurrent update")

    monkeypatch.setattr(products, "update_project", stale_update)

    with pytest.raises(PackagedProductJourneyError, match="changed before the goal update committed"):
        router.create(
            {"command": "Set current ProductProject goal: Revised accessible product goal"}
        )

    assert repository.get(product_project_identity(goal)).spec_version == 1
    assert ordinary.calls == []


@pytest.mark.parametrize(
    "command",
    [
        "Set current ProductProject goal",
        "Update current ProductProject goal:",
        "Встанови ціль поточного ProductProject:   ",
    ],
)
def test_refinement_rejects_missing_goal(command: str) -> None:
    with pytest.raises(PackagedProductJourneyError):
        packaged_product_goal_refinement(command)


def test_refinement_rejects_oversized_goal() -> None:
    command = "Set current ProductProject goal: " + "x" * 4001

    with pytest.raises(PackagedProductJourneyError, match="4000"):
        packaged_product_goal_refinement(command)
