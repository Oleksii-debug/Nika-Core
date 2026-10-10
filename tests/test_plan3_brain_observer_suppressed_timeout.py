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


def test_observer_that_ignores_cancellation_cannot_hold_brain_or_call_tools() -> None:
    calls: list[str] = []

    async def read(_arguments: dict[str, object]) -> str:
        calls.append("called")
        return "ok"

    class StubbornObserver:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()
            self.release = asyncio.Event()

        async def observe(self) -> WorldState:
            self.started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.cancelled.set()
                # Deliberately ignores the timeout until an external test
                # signal, reproducing an uncooperative plugin/adapter.
                await self.release.wait()
                return WorldState(frozenset({"done"}))

    async def scenario() -> None:
        observer = StubbornObserver()
        tools = ToolExecutor()
        tools.register(ToolSpec(tool_id="read", description="read"), read)
        brain = DeterministicBrain(planner=_OneActionPlanner(), tools=tools)
        task = asyncio.create_task(
            brain.run(
                run_id="noncooperative-observer",
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
        await observer.started.wait()
        # asyncio.wait (not wait_for) has an absolute outer test budget:
        # an old implementation that waits forever for adapter cancellation
        # will fail this assertion without deadlocking the test process.
        done, _ = await asyncio.wait({task}, timeout=1.0)
        observer.release.set()
        result = await asyncio.wait_for(task, timeout=1.0)
        await asyncio.sleep(0)
        assert task in done, "timeout must not await an uncooperative observer"
        assert observer.cancelled.is_set()
        assert result.error_code == DeterministicErrorCode.STATE_OBSERVATION_TIMEOUT
        assert result.completed_actions == ()
        assert calls == []

    asyncio.run(scenario())


def test_external_cancellation_still_propagates_and_cancels_observer() -> None:
    class WaitingObserver:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def observe(self) -> WorldState:
            self.started.set()
            try:
                await asyncio.Future()
            except asyncio.CancelledError:
                self.cancelled.set()
                raise

    async def scenario() -> None:
        observer = WaitingObserver()
        task = asyncio.create_task(
            DeterministicBrain._observe_state(observer, timeout_seconds=5)
        )
        await observer.started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("caller cancellation was swallowed")
        await asyncio.sleep(0)
        assert observer.cancelled.is_set()

    asyncio.run(scenario())
