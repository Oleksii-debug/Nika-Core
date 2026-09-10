from __future__ import annotations

import asyncio

from nika_core.intelligence.brain import DeterministicBrain
from nika_core.intelligence.contracts import (
    DeterministicAction,
    DeterministicGoal,
    DeterministicPlan,
    PlanStep,
    WorldState,
)
from nika_core.tools import ToolExecutor


class _FixedLargePlanner:
    def plan(
        self,
        *,
        state: WorldState,
        goal: DeterministicGoal,
        actions: tuple[DeterministicAction, ...],
    ) -> DeterministicPlan:
        del state, goal
        return DeterministicPlan(
            steps=tuple(PlanStep(action_id=action.action_id) for action in actions)
        )


def test_deterministic_brain_executes_1000_step_plan_with_exact_cumulative_budget() -> None:
    action_count = 1000
    actions = tuple(
        DeterministicAction(
            action_id=f"step-{index:04d}",
            requires=frozenset({f"state-{index - 1:04d}"}),
            adds=frozenset({f"state-{index:04d}"}),
        )
        for index in range(1, action_count + 1)
    )
    brain = DeterministicBrain(planner=_FixedLargePlanner(), tools=ToolExecutor())

    result = asyncio.run(
        brain.run(
            run_id="large-1000-step-proof",
            state=WorldState(frozenset({"state-0000"})),
            goal=DeterministicGoal(required=frozenset({"state-1000"})),
            actions=actions,
            max_steps=action_count,
            planning_timeout_seconds=10.0,
        )
    )

    assert result.ok
    assert result.error is None
    assert result.error_code is None
    assert result.replans == 0
    assert len(result.plan.steps) == action_count
    assert len(result.completed_actions) == action_count
    assert result.completed_actions[0] == "step-0001"
    assert result.completed_actions[-1] == "step-1000"
    assert len(set(result.completed_actions)) == action_count
    assert "state-1000" in result.final_state.facts
