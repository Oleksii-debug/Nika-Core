from __future__ import annotations

import asyncio

import pytest

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


class CountingPlanner:
    def __init__(self) -> None:
        self.calls = 0

    def plan(
        self,
        *,
        state: WorldState,
        goal: DeterministicGoal,
        actions: tuple[DeterministicAction, ...],
    ) -> DeterministicPlan:
        self.calls += 1
        return DeterministicPlan(steps=(PlanStep(action_id="finish"),))


_ACTIONS = (
    DeterministicAction(action_id="prepare", adds=frozenset({"prepared"})),
    DeterministicAction(
        action_id="finish",
        requires=frozenset({"prepared"}),
        adds=frozenset({"finished"}),
    ),
)


def _resume(*, max_steps: int, goal: str) -> tuple[object, CountingPlanner]:
    planner = CountingPlanner()
    brain = DeterministicBrain(planner=planner, tools=ToolExecutor())
    result = asyncio.run(
        brain.run(
            run_id="restart-budget",
            state=WorldState(facts=frozenset({"prepared"})),
            goal=DeterministicGoal(required=frozenset({goal})),
            actions=_ACTIONS,
            previously_completed_action_ids=("prepare",),
            max_steps=max_steps,
        )
    )
    return result, planner


def test_recovered_action_exhausts_total_budget_before_planner() -> None:
    result, planner = _resume(max_steps=1, goal="finished")
    assert not result.ok
    assert result.error_code is DeterministicErrorCode.PLAN_TOO_LONG
    assert result.completed_actions == ("prepare",)
    assert planner.calls == 0


def test_exact_budget_completed_checkpoint_is_terminal_without_replanning() -> None:
    result, planner = _resume(max_steps=1, goal="prepared")
    assert result.ok
    assert result.completed_actions == ("prepare",)
    assert result.final_state.facts == frozenset({"prepared"})
    assert planner.calls == 0
    assert result.planning_history == ()


def test_recovered_action_leaves_only_one_new_execution_slot() -> None:
    result, planner = _resume(max_steps=2, goal="finished")
    assert result.ok
    assert result.completed_actions == ("prepare", "finish")
    assert planner.calls == 1


def test_checkpoint_above_total_budget_is_not_accepted() -> None:
    result, planner = _resume(max_steps=0 + 1, goal="finished")
    assert not result.ok
    assert planner.calls == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_steps": True},
        {"max_steps": 1.5},
        {"max_replans": False},
        {"max_replans": 0.5},
        {"planning_timeout_seconds": float("nan")},
        {"planning_timeout_seconds": float("inf")},
        {"planning_timeout_seconds": 10**1000},
        {"observation_timeout_seconds": float("-inf")},
        {"observation_timeout_seconds": True},
    ],
)
def test_noncanonical_budgets_fail_before_planning(overrides: dict[str, object]) -> None:
    planner = CountingPlanner()
    brain = DeterministicBrain(planner=planner, tools=ToolExecutor())
    with pytest.raises(ValueError):
        asyncio.run(
            brain.run(
                run_id="unsafe-budget",
                state=WorldState(),
                goal=DeterministicGoal(required=frozenset({"finished"})),
                actions=_ACTIONS,
                **overrides,
            )
        )
    assert planner.calls == 0


def test_unresolved_effect_overrides_exact_budget_terminal_success() -> None:
    class UnresolvedJournal:
        def unresolved_operation_keys(self, *, task_id: str) -> tuple[str, ...]:
            assert task_id == "task-1"
            return ("pending-operation",)

    planner = CountingPlanner()
    brain = DeterministicBrain(
        planner=planner,
        tools=ToolExecutor(),
        effect_journal=UnresolvedJournal(),  # type: ignore[arg-type]
    )
    result = asyncio.run(
        brain.run(
            run_id="unsafe-restart",
            task_id="task-1",
            state=WorldState(facts=frozenset({"prepared"})),
            goal=DeterministicGoal(required=frozenset({"prepared"})),
            actions=_ACTIONS,
            previously_completed_action_ids=("prepare",),
            max_steps=1,
        )
    )
    assert not result.ok
    assert result.error_code is DeterministicErrorCode.SIDE_EFFECT_RECONCILIATION_REQUIRED
    assert planner.calls == 0
