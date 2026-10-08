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


class NeverDispatchTools:
    """Proof that invalid planner output cannot cross the effect boundary."""

    def __init__(self) -> None:
        self.execute_calls = 0

    def specs(self) -> tuple[()]:
        return ()

    async def execute(self, _call: object) -> None:
        self.execute_calls += 1
        raise AssertionError("malformed planner result reached a tool effect")


@pytest.mark.parametrize(
    "malformed",
    [
        None,
        {"steps": ()},
        DeterministicPlan(steps=[PlanStep(action_id="finish")]),
        DeterministicPlan(steps=(object(),)),
        DeterministicPlan(steps=(PlanStep(action_id=[]),)),
        DeterministicPlan(steps=(PlanStep(action_id="finish", tool_id=[]),)),
    ],
)
def test_malformed_planner_output_fails_closed_before_any_effect(malformed: object) -> None:
    class MalformedPlanner:
        def plan(self, *, state: object, goal: object, actions: object) -> object:
            return malformed

    tools = NeverDispatchTools()
    result = asyncio.run(
        DeterministicBrain(
            planner=MalformedPlanner(),  # type: ignore[arg-type]
            tools=tools,  # type: ignore[arg-type]
        ).run(
            run_id="untrusted-planner",
            state=WorldState(facts=frozenset({"prepared"})),
            goal=DeterministicGoal(required=frozenset({"finished"})),
            actions=(
                DeterministicAction(
                    action_id="finish",
                    requires=frozenset({"prepared"}),
                    adds=frozenset({"finished"}),
                    tool_id="effectful.tool",
                ),
            ),
            max_steps=2,
        )
    )

    assert result.ok is False
    assert result.error_code is DeterministicErrorCode.INVALID_PLAN
    assert result.completed_actions == ()
    assert result.final_state == WorldState(facts=frozenset({"prepared"}))
    assert result.planning_history == ()
    assert tools.execute_calls == 0
