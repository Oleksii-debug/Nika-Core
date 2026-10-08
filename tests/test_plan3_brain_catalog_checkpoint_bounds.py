"""Plan 3 Section 1 admission limits before planning and durable effects."""
from __future__ import annotations

import asyncio

import pytest

from nika_core.intelligence.brain import DeterministicBrain
from nika_core.intelligence.contracts import (
    DeterministicAction,
    DeterministicGoal,
    DeterministicPlan,
    PlanStep,
    WorldState,
)
from nika_core.tools import ToolExecutor


class CountingPlanner:
    def __init__(self) -> None:
        self.calls = 0

    def plan(self, *, state: object, goal: object, actions: object) -> DeterministicPlan:
        self.calls += 1
        return DeterministicPlan(steps=(PlanStep("finish"),))


@pytest.mark.parametrize("carrier", ("catalog", "checkpoint"))
def test_oversized_catalog_or_checkpoint_rejected_before_planning(carrier: str) -> None:
    planner = CountingPlanner()
    finish = DeterministicAction("finish", adds=frozenset({"done"}))
    actions = (finish,) * 10_001 if carrier == "catalog" else (finish,)
    completed = ("finish",) * 10_001 if carrier == "checkpoint" else ()
    with pytest.raises(ValueError, match="exceeds 10000 entries"):
        asyncio.run(
            DeterministicBrain(planner=planner, tools=ToolExecutor()).run(
                run_id="oversized-action-admission",
                state=WorldState(),
                goal=DeterministicGoal(required=frozenset({"done"})),
                actions=actions,
                previously_completed_action_ids=completed,
            )
        )
    assert planner.calls == 0


def test_small_catalog_preserves_canonical_plan_execution() -> None:
    planner = CountingPlanner()
    result = asyncio.run(
        DeterministicBrain(planner=planner, tools=ToolExecutor()).run(
            run_id="valid-action-catalog",
            state=WorldState(),
            goal=DeterministicGoal(required=frozenset({"done"})),
            actions=(DeterministicAction("finish", adds=frozenset({"done"})),),
        )
    )
    assert result.ok
    assert result.completed_actions == ("finish",)
    assert result.final_state.facts == frozenset({"done"})
    assert planner.calls == 1
