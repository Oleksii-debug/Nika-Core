from __future__ import annotations

import asyncio

import pytest

from nika_core.intelligence.brain import DeterministicBrain
from nika_core.tools import ToolCall, ToolResult, ToolSpec
from nika_core.intelligence.contracts import (
    DeterministicAction,
    DeterministicErrorCode,
    DeterministicPlanningError,
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

@pytest.mark.parametrize("problem", [RuntimeError("private-provider-details"), ValueError("bad")])
def test_unexpected_planner_error_has_typed_boundary_without_effect(problem: Exception) -> None:
    class FailingPlanner:
        def plan(self, *, state: object, goal: object, actions: object) -> object:
            raise problem

    tools = NeverDispatchTools()
    with pytest.raises(DeterministicPlanningError) as caught:
        asyncio.run(
            DeterministicBrain(
                planner=FailingPlanner(),  # type: ignore[arg-type]
                tools=tools,  # type: ignore[arg-type]
            ).run(
                run_id="unexpected-planner-failure",
                state=WorldState(facts=frozenset({"prepared"})),
                goal=DeterministicGoal(required=frozenset({"finished"})),
                actions=(DeterministicAction(action_id="finish", adds=frozenset({"finished"})),),
            )
        )
    assert caught.value.code is DeterministicErrorCode.PLANNER_FAILURE
    assert str(caught.value) == "deterministic planner adapter failed"
    assert "private-provider-details" not in str(caught.value)
    assert tools.execute_calls == 0


def test_planner_contract_error_keeps_specific_error_code() -> None:
    class ContractPlanner:
        def plan(self, *, state: object, goal: object, actions: object) -> object:
            raise DeterministicPlanningError(
                "goal has no plan", code=DeterministicErrorCode.NO_PLAN_FOUND
            )

    tools = NeverDispatchTools()
    with pytest.raises(DeterministicPlanningError) as caught:
        asyncio.run(
            DeterministicBrain(
                planner=ContractPlanner(),  # type: ignore[arg-type]
                tools=tools,  # type: ignore[arg-type]
            ).run(
                run_id="typed-planner-failure",
                state=WorldState(),
                goal=DeterministicGoal(required=frozenset({"finished"})),
                actions=(DeterministicAction(action_id="finish", adds=frozenset({"finished"})),),
            )
        )
    assert caught.value.code is DeterministicErrorCode.NO_PLAN_FOUND
    assert str(caught.value) == "goal has no plan"
    assert tools.execute_calls == 0


class RecordingReadOnlyTools:
    """Only canonical read-only tool calls may reach this recording boundary."""

    def __init__(self) -> None:
        self.calls: list[ToolCall] = []

    def specs(self) -> tuple[ToolSpec, ...]:
        return (ToolSpec(tool_id="read.demo", description="record read-only arguments"),)

    async def execute(self, call: ToolCall) -> ToolResult:
        self.calls.append(call)
        return ToolResult(call_id=call.call_id, tool_id=call.tool_id, output={"ok": True})


def test_planner_and_caller_cannot_mutate_authoritative_tool_arguments() -> None:
    original_arguments = {"nested": {"mode": "safe"}}
    action = DeterministicAction(
        action_id="read",
        adds=frozenset({"done"}),
        tool_id="read.demo",
        arguments=original_arguments,
    )

    class MutatingPlanner:
        def plan(self, *, state: object, goal: object, actions: object) -> DeterministicPlan:
            del state, goal
            catalog = actions  # runtime adapter receives detached action records
            catalog[0].arguments["nested"]["mode"] = "planner-injected"  # type: ignore[index]
            return DeterministicPlan(steps=(PlanStep("read", "read.demo"),))

    class MutatingObserver:
        async def observe(self) -> WorldState:
            # Simulate another caller mutating its original action after planning.
            original_arguments["nested"]["mode"] = "caller-injected"
            return WorldState()

    tools = RecordingReadOnlyTools()
    result = asyncio.run(
        DeterministicBrain(planner=MutatingPlanner(), tools=tools).run(  # type: ignore[arg-type]
            run_id="planner-isolated-arguments",
            state=WorldState(),
            goal=DeterministicGoal(required=frozenset({"done"})),
            actions=(action,),
            state_observer=MutatingObserver(),
        )
    )
    assert result.ok
    assert result.completed_actions == ("read",)
    assert len(tools.calls) == 1
    assert tools.calls[0].arguments == {"nested": {"mode": "safe"}}
    assert original_arguments["nested"]["mode"] == "caller-injected"


def test_planner_cannot_change_frozen_state_or_goal_to_authorize_tool() -> None:
    state = WorldState()
    goal = DeterministicGoal(required=frozenset({"real-goal"}))

    class ForgingPlanner:
        def plan(self, *, state: WorldState, goal: DeterministicGoal,
                 actions: tuple[DeterministicAction, ...]) -> DeterministicPlan:
            del actions
            object.__setattr__(state, "facts", frozenset({"forged-precondition"}))
            object.__setattr__(goal, "required", frozenset({"done"}))
            return DeterministicPlan(steps=(PlanStep("read", "read.demo"),))

    tools = RecordingReadOnlyTools()
    result = asyncio.run(
        DeterministicBrain(planner=ForgingPlanner(), tools=tools).run(
            run_id="planner-isolated-authority",
            state=state,
            goal=goal,
            actions=(
                DeterministicAction(
                    action_id="read",
                    requires=frozenset({"forged-precondition"}),
                    adds=frozenset({"done"}),
                    tool_id="read.demo",
                ),
            ),
        )
    )
    assert not result.ok
    assert result.error_code is DeterministicErrorCode.INVALID_PLAN
    assert result.completed_actions == ()
    assert state.facts == frozenset()
    assert goal.required == frozenset({"real-goal"})
    assert tools.calls == []


def test_uncopyable_action_payload_is_rejected_before_planning_or_tool_effect() -> None:
    class Uncopyable:
        def __deepcopy__(self, memo: object) -> object:
            raise RuntimeError("untrusted-copy-behavior")

    class NoPlanner:
        def plan(self, *, state: object, goal: object, actions: object) -> None:
            raise AssertionError("planner must not receive unsafe mutable inputs")

    tools = RecordingReadOnlyTools()
    with pytest.raises(ValueError, match="cannot be detached safely"):
        asyncio.run(
            DeterministicBrain(planner=NoPlanner(), tools=tools).run(  # type: ignore[arg-type]
                run_id="planner-unsafe-snapshot",
                state=WorldState(),
                goal=DeterministicGoal(required=frozenset({"done"})),
                actions=(
                    DeterministicAction(
                        action_id="read",
                        adds=frozenset({"done"}),
                        tool_id="read.demo",
                        arguments={"unsafe": Uncopyable()},
                    ),
                ),
            )
        )
    assert tools.calls == []
