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


def _planner_must_not_start() -> object:
    raise AssertionError("planner engine must not start for a deterministic precheck result")


def test_already_satisfied_goal_does_not_start_planner_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    planner = UnifiedPlanningAdapter()
    monkeypatch.setattr(planner, "_shortcuts", _planner_must_not_start)

    plan = planner.plan(
        state=WorldState(frozenset({"done"})),
        goal=DeterministicGoal(required=frozenset({"done"})),
        actions=(),
    )

    assert plan.steps == ()


def test_unaddable_required_fact_fails_before_planner_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    planner = UnifiedPlanningAdapter()
    monkeypatch.setattr(planner, "_shortcuts", _planner_must_not_start)

    with pytest.raises(DeterministicPlanningError) as raised:
        planner.plan(
            state=WorldState(),
            goal=DeterministicGoal(required=frozenset({"unreachable"})),
            actions=(DeterministicAction(action_id="irrelevant", adds=frozenset({"other"})),),
        )

    assert raised.value.code is DeterministicErrorCode.GOAL_UNREACHABLE
    assert "unreachable" in str(raised.value)


def test_unremovable_forbidden_fact_fails_before_planner_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    planner = UnifiedPlanningAdapter()
    monkeypatch.setattr(planner, "_shortcuts", _planner_must_not_start)

    with pytest.raises(DeterministicPlanningError) as raised:
        planner.plan(
            state=WorldState(frozenset({"blocked"})),
            goal=DeterministicGoal(forbidden=frozenset({"blocked"})),
            actions=(DeterministicAction(action_id="irrelevant", adds=frozenset({"other"})),),
        )

    assert raised.value.code is DeterministicErrorCode.GOAL_UNREACHABLE
    assert "blocked" in str(raised.value)
