"""Plan 3 §1: bound unexecuted planner history under repeated state drift."""

from __future__ import annotations

import asyncio

import pytest

import nika_core.intelligence.brain as brain_module
from nika_core.intelligence.brain import DeterministicBrain
from nika_core.intelligence.contracts import (
    DeterministicAction,
    DeterministicErrorCode,
    DeterministicGoal,
    DeterministicPlan,
    PlanStep,
    WorldState,
)
from nika_core.tools import ToolExecutor


class RepeatingPlanner:
    def __init__(self) -> None:
        self.calls = 0

    def plan(
        self, *, state: WorldState, goal: DeterministicGoal,
        actions: tuple[DeterministicAction, ...],
    ) -> DeterministicPlan:
        self.calls += 1
        return DeterministicPlan(steps=(PlanStep("finish"), PlanStep("after")))


class DriftOnce:
    def __init__(self) -> None:
        self.calls = 0

    async def observe(self) -> WorldState:
        self.calls += 1
        return WorldState(facts=frozenset({"changed"}))


_ACTIONS = (
    DeterministicAction(action_id="finish", adds=frozenset({"done"})),
    DeterministicAction(action_id="after", adds=frozenset({"after"})),
)
_GOAL = DeterministicGoal(required=frozenset({"done"}))


def test_repeated_replans_cannot_accumulate_unbounded_plan_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Small test budget exercises the same production admission path without
    # allocating a large plan. Drift requests a new valid plan before effects.
    monkeypatch.setattr(brain_module, "_MAX_PLANNING_HISTORY_STEPS", 3)
    planner = RepeatingPlanner()
    observer = DriftOnce()
    result = asyncio.run(
        DeterministicBrain(planner=planner, tools=ToolExecutor()).run(
            run_id="replan-history-limit",
            state=WorldState(),
            goal=_GOAL,
            actions=_ACTIONS,
            state_observer=observer,
            max_steps=4,
            max_replans=4,
        )
    )
    assert not result.ok
    assert result.error_code == DeterministicErrorCode.PLANNER_RESOURCE_LIMIT
    assert result.completed_actions == ()
    assert result.final_state.facts == frozenset({"changed"})
    assert len(result.planning_history) == 1
    assert len(result.planning_history[0].steps) == 2
    assert planner.calls == 2
    assert observer.calls == 1


def test_valid_plan_at_history_budget_remains_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(brain_module, "_MAX_PLANNING_HISTORY_STEPS", 2)
    planner = RepeatingPlanner()
    result = asyncio.run(
        DeterministicBrain(planner=planner, tools=ToolExecutor()).run(
            run_id="replan-history-positive",
            state=WorldState(),
            goal=_GOAL,
            actions=_ACTIONS,
            max_steps=4,
        )
    )
    assert result.ok
    assert result.completed_actions == ("finish", "after")
    assert result.final_state.facts == frozenset({"done", "after"})
    assert planner.calls == 1
