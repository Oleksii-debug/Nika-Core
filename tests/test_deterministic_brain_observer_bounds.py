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
from nika_core.tools import ToolExecutor, ToolSpec


class _OneActionPlanner:
    def plan(self, *, state, goal, actions):
        del state, goal
        action = actions[0]
        return DeterministicPlan(
            steps=(PlanStep(action_id=action.action_id, tool_id=action.tool_id),)
        )


def test_state_observation_timeout_blocks_tool_execution() -> None:
    handler_calls = 0

    async def read(_arguments: dict[str, object]) -> object:
        nonlocal handler_calls
        handler_calls += 1
        return "read"

    class HungObserver:
        async def observe(self) -> WorldState:
            await asyncio.sleep(0.05)
            return WorldState()

    tools = ToolExecutor()
    tools.register(ToolSpec(tool_id="read", description="read"), read)
    action = DeterministicAction(
        action_id="read-and-finish",
        adds=frozenset({"done"}),
        tool_id="read",
    )
    result = asyncio.run(
        DeterministicBrain(planner=_OneActionPlanner(), tools=tools).run(
            run_id="observer-timeout",
            state=WorldState(),
            goal=DeterministicGoal(required=frozenset({"done"})),
            actions=(action,),
            state_observer=HungObserver(),
            observation_timeout_seconds=0.01,
        )
    )

    assert result.error_code == DeterministicErrorCode.STATE_OBSERVATION_TIMEOUT
    assert result.completed_actions == ()
    assert handler_calls == 0


def test_state_observer_failure_after_action_preserves_completed_evidence() -> None:
    class FailingFinalObserver:
        def __init__(self) -> None:
            self.calls = 0

        async def observe(self) -> WorldState:
            self.calls += 1
            if self.calls == 1:
                return WorldState()
            raise RuntimeError("observer unavailable")

    observer = FailingFinalObserver()
    action = DeterministicAction(action_id="finish", adds=frozenset({"done"}))
    result = asyncio.run(
        DeterministicBrain(planner=_OneActionPlanner(), tools=ToolExecutor()).run(
            run_id="observer-failure",
            state=WorldState(),
            goal=DeterministicGoal(required=frozenset({"done"})),
            actions=(action,),
            state_observer=observer,
            observation_timeout_seconds=0.1,
        )
    )

    assert result.error_code == DeterministicErrorCode.STATE_OBSERVATION_FAILED
    assert result.completed_actions == ("finish",)
    assert result.final_state == WorldState(frozenset({"done"}))
    assert observer.calls == 2


def test_invalid_observation_timeout_is_rejected() -> None:
    brain = DeterministicBrain(planner=_OneActionPlanner(), tools=ToolExecutor())

    try:
        asyncio.run(
            brain.run(
                run_id="invalid-observer-timeout",
                state=WorldState(),
                goal=DeterministicGoal(),
                actions=(),
                observation_timeout_seconds=0,
            )
        )
    except ValueError as exc:
        assert "observation_timeout_seconds" in str(exc)
    else:  # pragma: no cover - invalid budget must fail at API boundary
        raise AssertionError("zero observation timeout was accepted")
