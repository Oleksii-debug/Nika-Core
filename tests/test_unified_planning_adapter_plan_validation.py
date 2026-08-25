from __future__ import annotations

import pytest

from nika_core.intelligence.contracts import (
    DeterministicAction,
    DeterministicErrorCode,
    DeterministicGoal,
    DeterministicPlanningError,
    WorldState,
)
from nika_core.intelligence.unified_planning_adapter import UnifiedPlanningAdapter


def test_validated_plan_skips_action_whose_effect_is_already_satisfied() -> None:
    plan = UnifiedPlanningAdapter._validated_plan(
        state=WorldState(frozenset({"source-configured", "pages-fetched"})),
        goal=DeterministicGoal(required=frozenset({"candidates-ready"})),
        planned_actions=(
            DeterministicAction(
                action_id="fetch-pages",
                requires=frozenset({"source-configured"}),
                adds=frozenset({"pages-fetched"}),
                tool_id="research.fetch",
            ),
            DeterministicAction(
                action_id="filter-pages",
                requires=frozenset({"pages-fetched"}),
                adds=frozenset({"candidates-ready"}),
                tool_id="research.filter",
            ),
        ),
    )

    assert tuple(step.action_id for step in plan.steps) == ("filter-pages",)
    assert tuple(step.tool_id for step in plan.steps) == ("research.filter",)


def test_validated_plan_keeps_remove_effect_that_changes_state() -> None:
    plan = UnifiedPlanningAdapter._validated_plan(
        state=WorldState(frozenset({"temporary", "ready"})),
        goal=DeterministicGoal(
            required=frozenset({"ready"}),
            forbidden=frozenset({"temporary"}),
        ),
        planned_actions=(
            DeterministicAction(
                action_id="clear-temporary",
                requires=frozenset({"temporary"}),
                removes=frozenset({"temporary"}),
            ),
        ),
    )

    assert tuple(step.action_id for step in plan.steps) == ("clear-temporary",)


def test_validated_plan_rejects_inapplicable_solver_action() -> None:
    with pytest.raises(DeterministicPlanningError) as exc_info:
        UnifiedPlanningAdapter._validated_plan(
            state=WorldState(),
            goal=DeterministicGoal(required=frozenset({"done"})),
            planned_actions=(
                DeterministicAction(
                    action_id="finish",
                    requires=frozenset({"ready"}),
                    adds=frozenset({"done"}),
                ),
            ),
        )

    assert exc_info.value.code == DeterministicErrorCode.INVALID_PLAN
    assert str(exc_info.value) == "planner returned an inapplicable action: finish"


def test_validated_plan_rejects_solver_output_that_misses_goal() -> None:
    with pytest.raises(DeterministicPlanningError) as exc_info:
        UnifiedPlanningAdapter._validated_plan(
            state=WorldState(frozenset({"ready"})),
            goal=DeterministicGoal(required=frozenset({"done"})),
            planned_actions=(
                DeterministicAction(
                    action_id="redundant-ready",
                    adds=frozenset({"ready"}),
                ),
            ),
        )

    assert exc_info.value.code == DeterministicErrorCode.INVALID_PLAN
    assert str(exc_info.value) == "planner returned a plan that does not satisfy the goal"
