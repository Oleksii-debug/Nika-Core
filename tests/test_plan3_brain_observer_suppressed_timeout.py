"""Adversarial observer cancellation must not forge fresh world-state evidence."""

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
        return DeterministicPlan(
            steps=(PlanStep(action_id=actions[0].action_id, tool_id=actions[0].tool_id),)
        )


class _CancellationSuppressingObserver:
    def __init__(self, reported_state: WorldState) -> None:
        self.reported_state = reported_state
        self.cancelled = False

    async def observe(self) -> WorldState:
        try:
            await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            # A replaceable observer may swallow task cancellation and report
            # a stale/forged success after the authoritative absolute deadline.
            self.cancelled = True
            await asyncio.sleep(0)
        return self.reported_state


def test_late_observation_cannot_authorize_read_tool() -> None:
    calls: list[str] = []

    async def read(_arguments: dict[str, object]) -> str:
        calls.append("called")
        return "ok"

    tools = ToolExecutor()
    tools.register(ToolSpec(tool_id="read", description="read"), read)
    observer = _CancellationSuppressingObserver(WorldState())
    result = asyncio.run(
        DeterministicBrain(planner=_OneActionPlanner(), tools=tools).run(
            run_id="suppressed-observation-deadline",
            state=WorldState(),
            goal=DeterministicGoal(required=frozenset({"done"})),
            actions=(
                DeterministicAction(
                    action_id="finish", adds=frozenset({"done"}), tool_id="read"
                ),
            ),
            state_observer=observer,
            observation_timeout_seconds=0.005,
        )
    )
    assert observer.cancelled
    assert calls == []
    assert result.completed_actions == ()
    assert result.error_code == DeterministicErrorCode.STATE_OBSERVATION_TIMEOUT


def test_late_observation_cannot_validate_recovered_terminal_state() -> None:
    observer = _CancellationSuppressingObserver(
        WorldState(frozenset({"done"}))
    )
    result = asyncio.run(
        DeterministicBrain(planner=_OneActionPlanner(), tools=ToolExecutor()).run(
            run_id="suppressed-recovery-deadline",
            state=WorldState(frozenset({"done"})),
            goal=DeterministicGoal(required=frozenset({"done"})),
            actions=(),
            state_observer=observer,
            observation_timeout_seconds=0.005,
        )
    )
    assert observer.cancelled
    assert result.completed_actions == ()
    assert result.error_code == DeterministicErrorCode.STATE_OBSERVATION_TIMEOUT


def test_timely_observation_still_allows_normal_read_execution() -> None:
    calls: list[str] = []

    async def read(_arguments: dict[str, object]) -> str:
        calls.append("called")
        return "ok"

    class TimelyObserver:
        async def observe(self) -> WorldState:
            return WorldState(frozenset({"done"})) if calls else WorldState()

    tools = ToolExecutor()
    tools.register(ToolSpec(tool_id="read", description="read"), read)
    result = asyncio.run(
        DeterministicBrain(planner=_OneActionPlanner(), tools=tools).run(
            run_id="timely-observation-deadline",
            state=WorldState(),
            goal=DeterministicGoal(required=frozenset({"done"})),
            actions=(
                DeterministicAction(
                    action_id="finish", adds=frozenset({"done"}), tool_id="read"
                ),
            ),
            state_observer=TimelyObserver(),
            observation_timeout_seconds=1,
        )
    )
    assert result.ok
    assert result.completed_actions == ("finish",)
    assert calls == ["called"]
