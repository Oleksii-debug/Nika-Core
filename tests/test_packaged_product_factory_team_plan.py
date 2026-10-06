from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
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
from nika_core.product_factory_packaged_planning import (
    TEAM_PLAN_REF_PREFIX,
    PackagedProductFactoryPlanningError,
    PackagedProductFactoryTeamPlanner,
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
    PackagedProductFactoryTeamPlanner,
    OrdinaryHandler,
]:
    store = SQLiteStore(path)
    store.initialize()
    repository = ProductProjectRepository(store)
    selection = PackagedProductSelectionStore(store)
    planner = PackagedProductFactoryTeamPlanner(repository)
    ordinary = OrdinaryHandler()
    router = PackagedProductCommandRouter(
        products=ProductProjectCommandService(repository),
        ordinary_handler=ordinary,
        selection_store=selection,
        team_planner=planner,
    )
    return router, repository, selection, planner, ordinary


def test_packaged_factory_plan_persists_and_recovers_after_restart(tmp_path: Path) -> None:
    database = tmp_path / "план Product Factory з пробілами.db"
    goal = "Створи застосунок для доступного обліку рахунків"
    project_id = product_project_identity(goal)

    first, first_repository, first_selection, _first_planner, first_ordinary = _durable_router(
        database
    )
    first.create({"command": goal})
    planned = first.create({"command": "Plan current ProductProject"})

    persisted = first_repository.get(project_id)
    owned_refs = tuple(
        ref for ref in persisted.spec.team_refs if ref.startswith(TEAM_PLAN_REF_PREFIX)
    )
    assert persisted.spec_version == 2
    assert len(owned_refs) == 1
    assert planned.status == "completed"
    assert planned.focus_id == "tasks-heading"
    assert project_id in planned.message
    assert "spec version 2" in planned.message
    assert "permission ceiling: read_project" in planned.message
    assert "worker dispatch: not started" in planned.message
    assert first_selection.load() == project_id
    assert first_ordinary.calls == []

    restarted, restarted_repository, restarted_selection, _planner, restarted_ordinary = (
        _durable_router(database)
    )
    recovered = restarted.create({"command": "Show current Product Factory plan"})

    assert recovered == planned
    assert restarted_repository.get(project_id).spec_version == 2
    assert restarted_selection.load() == project_id
    assert restarted_ordinary.calls == []


def test_repeated_explicit_factory_plan_is_idempotent(tmp_path: Path) -> None:
    database = tmp_path / "idempotent factory plan.db"
    goal = "Create product application for accessible invoice review"
    project_id = product_project_identity(goal)
    router, repository, _selection, _planner, ordinary = _durable_router(database)
    router.create({"command": goal})

    first = router.create({"command": "Plan current Product Factory"})
    after_first = repository.get(project_id)
    second = router.create({"command": "сплануй поточний ProductProject"})
    after_second = repository.get(project_id)

    assert first == second
    assert after_first == after_second
    assert after_second.spec_version == 2
    assert len(repository.spec_history(project_id)) == 2
    assert ordinary.calls == []


@pytest.mark.parametrize(
    "command",
    [
        "Plan current ProductProject",
        "Plan current Product Factory",
        "сплануй поточний ProductProject",
        "сплануй Product Factory для поточного ProductProject",
        "Show current Product Factory plan",
        "Покажи поточний план Product Factory",
    ],
)
def test_factory_plan_commands_without_current_project_fail_closed(
    tmp_path: Path,
    command: str,
) -> None:
    router, _repository, selection, _planner, ordinary = _durable_router(
        tmp_path / "no current.db"
    )

    with pytest.raises(PackagedProductJourneyError, match="не вибрано"):
        router.create({"command": command})

    assert selection.load() is None
    assert ordinary.calls == []


def test_show_factory_plan_before_explicit_planning_fails_closed(tmp_path: Path) -> None:
    goal = "Create product application for expense review"
    router, repository, _selection, _planner, ordinary = _durable_router(
        tmp_path / "not planned.db"
    )
    router.create({"command": goal})
    before = repository.get(product_project_identity(goal))

    with pytest.raises(PackagedProductJourneyError, match="ще не збережено"):
        router.create({"command": "Current Product Factory plan"})

    assert repository.get(before.project_id) == before
    assert ordinary.calls == []


def test_factory_plan_detects_stale_spec_and_replans_explicitly(tmp_path: Path) -> None:
    database = tmp_path / "stale plan.db"
    goal = "Create product application for invoice review"
    project_id = product_project_identity(goal)
    router, repository, _selection, _planner, ordinary = _durable_router(database)
    router.create({"command": goal})
    router.create({"command": "Plan current ProductProject"})
    planned = repository.get(project_id)
    original_owned_ref = next(
        ref for ref in planned.spec.team_refs if ref.startswith(TEAM_PLAN_REF_PREFIX)
    )

    changed = repository.update_spec(
        project_id,
        replace(
            planned.spec,
            desired_outcome="Invoice review with explicit export acceptance",
            team_refs=(*planned.spec.team_refs, "external-team:alpha"),
        ),
        expected_row_version=planned.row_version,
        change_reason="test external ProductProject revision",
    )
    assert changed.spec_version == 3

    restarted, restarted_repository, _selection2, _planner2, _ordinary2 = _durable_router(
        database
    )
    with pytest.raises(PackagedProductJourneyError, match="застарів"):
        restarted.create({"command": "Show current Product Factory plan"})

    replanned = restarted.create({"command": "Plan current ProductProject"})
    current = restarted_repository.get(project_id)
    owned_refs = tuple(
        ref for ref in current.spec.team_refs if ref.startswith(TEAM_PLAN_REF_PREFIX)
    )
    assert current.spec_version == 4
    assert current.spec.team_refs[0] == "external-team:alpha"
    assert len(owned_refs) == 1
    assert owned_refs[0] != original_owned_ref
    assert "spec version 4" in replanned.message
    assert ordinary.calls == []


def test_multiple_packaged_plan_bindings_fail_closed(tmp_path: Path) -> None:
    goal = "Create product application for invoice review"
    project_id = product_project_identity(goal)
    router, repository, _selection, planner, _ordinary = _durable_router(
        tmp_path / "ambiguous plan.db"
    )
    router.create({"command": goal})
    current = repository.get(project_id)
    repository.update_spec(
        project_id,
        replace(
            current.spec,
            team_refs=(
                TEAM_PLAN_REF_PREFIX + "one:aaa",
                TEAM_PLAN_REF_PREFIX + "two:bbb",
            ),
        ),
        expected_row_version=current.row_version,
        change_reason="test ambiguous packaged plan bindings",
    )

    with pytest.raises(PackagedProductFactoryPlanningError, match="multiple"):
        planner.inspect(project_id)
    with pytest.raises(PackagedProductFactoryPlanningError, match="multiple"):
        planner.plan(project_id)


def test_packaged_factory_team_plan_cannot_grant_execution_permissions(tmp_path: Path) -> None:
    goal = "Create product application for accessible invoice review"
    project_id = product_project_identity(goal)
    router, _repository, _selection, planner, ordinary = _durable_router(
        tmp_path / "bounded plan.db"
    )
    router.create({"command": goal})

    result = planner.plan(project_id)

    assert result.plan.permission_ceiling == frozenset({"read_project"})
    assert result.independent_review_count == 1
    assert result.plan.roles
    assert all(role.permissions <= frozenset({"read_project"}) for role in result.plan.roles)
    assert not any(
        permission in {"write_source", "run_tests", "build_release", "update_project"}
        for role in result.plan.roles
        for permission in role.permissions
    )
    assert ordinary.calls == []
