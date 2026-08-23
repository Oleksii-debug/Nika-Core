from __future__ import annotations

import asyncio

from nika_core.intelligence.brain import DeterministicBrain
from nika_core.intelligence.contracts import (
    DeterministicAction,
    DeterministicErrorCode,
    DeterministicGoal,
    DeterministicPlan,
    PlanStep,
    WorldState,
)
from nika_core.tools import ToolExecutor, ToolRisk, ToolSpec


class _OneActionPlanner:
    def plan(self, *, state, goal, actions):
        del state, goal
        action = actions[0]
        return DeterministicPlan(
            steps=(PlanStep(action_id=action.action_id, tool_id=action.tool_id),)
        )


def test_non_read_only_tool_requires_durable_effect_journal_before_handler() -> None:
    called = False

    async def write(_arguments: dict[str, object]) -> object:
        nonlocal called
        called = True
        return "written"

    tools = ToolExecutor()
    tools.register(
        ToolSpec(
            tool_id="write.result",
            description="write",
            risk=ToolRisk.LOCAL_WRITE,
        ),
        write,
    )
    action = DeterministicAction(
        action_id="write-result",
        adds=frozenset({"done"}),
        tool_id="write.result",
    )
    result = asyncio.run(
        DeterministicBrain(planner=_OneActionPlanner(), tools=tools).run(
            run_id="unsafe-effect",
            state=WorldState(),
            goal=DeterministicGoal(required=frozenset({"done"})),
            actions=(action,),
        )
    )

    assert result.error_code == DeterministicErrorCode.SIDE_EFFECT_JOURNAL_REQUIRED
    assert result.completed_actions == ()
    assert result.final_state == WorldState()
    assert called is False
