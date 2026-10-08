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
from nika_core.tools import ToolCall, ToolResult, ToolSpec


class SingleStepPlanner:
    def __init__(self) -> None:
        self.calls = 0

    def plan(self, *, state: object, goal: object, actions: object) -> DeterministicPlan:
        self.calls += 1
        return DeterministicPlan(steps=(PlanStep("finish", "read.demo"),))


class RecordingTools:
    def __init__(self) -> None:
        self.calls: list[ToolCall] = []

    def specs(self) -> tuple[ToolSpec, ...]:
        return (ToolSpec(tool_id="read.demo", description="record read-only input"),)

    async def execute(self, call: ToolCall) -> ToolResult:
        self.calls.append(call)
        return ToolResult(call_id=call.call_id, tool_id=call.tool_id, output={"ok": True})


def run_action(*, arguments: object, planner: SingleStepPlanner,
               tools: RecordingTools) -> object:
    return asyncio.run(
        DeterministicBrain(planner=planner, tools=tools).run(
            run_id="inert-args",
            state=WorldState(),
            goal=DeterministicGoal(required=frozenset({"done"})),
            actions=(
                DeterministicAction(
                    action_id="finish", adds=frozenset({"done"}),
                    tool_id="read.demo", arguments=arguments,  # type: ignore[arg-type]
                ),
            ),
        )
    )


def test_custom_deepcopy_never_invoked_during_action_admission() -> None:
    class AliasedPayload:
        attempts = 0

        def __deepcopy__(self, memo: object) -> object:
            del memo
            self.attempts += 1
            return self  # Would retain caller authority despite deepcopy.

    attack = AliasedPayload()
    planner, tools = SingleStepPlanner(), RecordingTools()
    with pytest.raises(ValueError, match="cannot be detached safely"):
        run_action(arguments={"nested": [{"behavioral": attack}]}, planner=planner, tools=tools)
    assert attack.attempts == 0
    assert planner.calls == 0
    assert tools.calls == []


@pytest.mark.parametrize(
    "payload",
    [
        {"bad": object()},
        {"bad": float("nan")},
        {"bad": float("inf")},
        {"bad": {1: "non-string-key"}},
        {"bad": {1, 2}},
        {"bad": bytes([0, 1])},
        {"bad": [object()]},
    ],
)
def test_non_json_argument_is_rejected_before_planning(payload: object) -> None:
    planner, tools = SingleStepPlanner(), RecordingTools()
    with pytest.raises(ValueError, match="cannot be detached safely"):
        run_action(arguments=payload, planner=planner, tools=tools)
    assert planner.calls == 0
    assert tools.calls == []


def test_behavioral_dict_subclass_is_not_inspected() -> None:
    class HostileMapping(dict):
        def items(self) -> object:
            raise AssertionError("untrusted mapping method called")

        def __deepcopy__(self, memo: object) -> object:
            raise AssertionError("untrusted copy method called")

    planner, tools = SingleStepPlanner(), RecordingTools()
    with pytest.raises(ValueError, match="cannot be detached safely"):
        run_action(arguments=HostileMapping({"x": 1}), planner=planner, tools=tools)
    assert planner.calls == 0
    assert tools.calls == []


def test_cycle_and_excessive_nesting_fail_closed_without_planner() -> None:
    cyclic: list[object] = []
    cyclic.append(cyclic)
    nested: object = None
    for _ in range(36):
        nested = [nested]
    for payload in ({"cycle": cyclic}, {"depth": nested}):
        planner, tools = SingleStepPlanner(), RecordingTools()
        with pytest.raises(ValueError, match="cannot be detached safely"):
            run_action(arguments=payload, planner=planner, tools=tools)
        assert planner.calls == 0
        assert tools.calls == []


def test_plain_nested_arguments_preserve_normal_execution() -> None:
    arguments = {"nested": {"numbers": [1, 2], "flags": (True, None)}, "weight": 1.25}
    planner, tools = SingleStepPlanner(), RecordingTools()
    result = run_action(arguments=arguments, planner=planner, tools=tools)
    assert result.ok  # type: ignore[attr-defined]
    assert result.completed_actions == ("finish",)  # type: ignore[attr-defined]
    assert planner.calls == 1
    assert len(tools.calls) == 1
    assert tools.calls[0].arguments == arguments
    assert tools.calls[0].approved is False
