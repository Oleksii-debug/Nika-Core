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


@pytest.mark.parametrize(
    "payload",
    [
        {"text": "x" * (256 * 1024)},
        {"unicode": "Ї" * (128 * 1024)},
        {"text": "\ud800"},
        {"\ud800": "value"},
        {"huge_integer": 2 ** 4097},
        {"nested": [{"text": "a" * (256 * 1024)}]},
    ],
)
def test_oversized_or_unencodable_arguments_never_reach_planner_or_tools(
    payload: object,
) -> None:
    planner, tools = SingleStepPlanner(), RecordingTools()
    with pytest.raises(ValueError, match="cannot be detached safely"):
        run_action(arguments=payload, planner=planner, tools=tools)
    assert planner.calls == 0
    assert tools.calls == []


def test_exact_nested_utf8_size_budget_preserves_valid_read_only_execution() -> None:
    # Count dict key bytes as well as values; remain on the 256 KiB ceiling.
    payload = {"text": "x" * (256 * 1024 - len("text"))}
    planner, tools = SingleStepPlanner(), RecordingTools()
    result = run_action(arguments=payload, planner=planner, tools=tools)
    assert result.ok  # type: ignore[attr-defined]
    assert len(tools.calls) == 1
    assert tools.calls[0].arguments == payload
    assert tools.calls[0].approved is False


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


@pytest.mark.parametrize("poisoned_field", ["state", "goal", "action_facts", "tool_id"])
def test_behaving_frozen_record_fields_rejected_before_deepcopy(
    poisoned_field: str,
) -> None:
    class HostileFact:
        deepcopy_calls = 0

        def __deepcopy__(self, memo: object) -> object:
            del memo
            self.deepcopy_calls += 1
            return self

    class HostileToolId(str):
        deepcopy_calls = 0

        def __deepcopy__(self, memo: object) -> object:
            del memo
            self.deepcopy_calls += 1
            return self

    state = WorldState()
    goal = DeterministicGoal(required=frozenset({"done"}))
    action = DeterministicAction(
        action_id="finish", adds=frozenset({"done"}), tool_id="read.demo",
    )
    attack = HostileFact() if poisoned_field != "tool_id" else HostileToolId("read.demo")
    if poisoned_field == "state":
        object.__setattr__(state, "facts", frozenset({attack}))
    elif poisoned_field == "goal":
        object.__setattr__(goal, "required", frozenset({attack}))
    elif poisoned_field == "action_facts":
        object.__setattr__(action, "requires", frozenset({attack}))
    else:
        object.__setattr__(action, "tool_id", attack)

    planner, tools = SingleStepPlanner(), RecordingTools()
    with pytest.raises(ValueError, match="canonical|cannot be detached safely"):
        asyncio.run(
            DeterministicBrain(planner=planner, tools=tools).run(
                run_id="untrusted-record", state=state, goal=goal, actions=(action,),
            )
        )
    assert attack.deepcopy_calls == 0
    assert planner.calls == 0
    assert tools.calls == []


def test_planner_must_not_invoke_behavioral_step_deepcopy() -> None:
    class BehavioralStepId(str):
        deepcopy_calls = 0

        def __deepcopy__(self, memo: object) -> object:
            del memo
            self.deepcopy_calls += 1
            raise AssertionError("planner carrier deepcopy executed")

    poisoned_id = BehavioralStepId("finish")

    class BehavioralStepPlanner(SingleStepPlanner):
        def plan(self, *, state: object, goal: object, actions: object) -> DeterministicPlan:
            self.calls += 1
            return DeterministicPlan(
                steps=(PlanStep(action_id=poisoned_id, tool_id="read.demo"),)
            )

    planner, tools = BehavioralStepPlanner(), RecordingTools()
    result = run_action(arguments={"safe": True}, planner=planner, tools=tools)
    assert result.error_code is DeterministicErrorCode.INVALID_PLAN
    assert poisoned_id.deepcopy_calls == 0
    assert planner.calls == 1
    assert tools.calls == []

def test_oversized_planner_result_is_rejected_before_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nika_core.intelligence.brain as brain_module

    class OversizedPlanner(SingleStepPlanner):
        def plan(self, *, state: object, goal: object, actions: object) -> DeterministicPlan:
            self.calls += 1
            return DeterministicPlan(
                steps=(PlanStep(action_id="finish", tool_id="read.demo"),) * 101
            )

    original_deepcopy = brain_module.deepcopy
    copied_plans: list[DeterministicPlan] = []

    def guarded_deepcopy(value: object) -> object:
        if type(value) is DeterministicPlan:
            copied_plans.append(value)
            raise AssertionError("oversized planner output was snapshotted")
        return original_deepcopy(value)

    monkeypatch.setattr(brain_module, "deepcopy", guarded_deepcopy)
    planner, tools = OversizedPlanner(), RecordingTools()
    result = run_action(arguments={"safe": True}, planner=planner, tools=tools)
    assert result.error_code is DeterministicErrorCode.PLAN_TOO_LONG
    assert copied_plans == []
    assert planner.calls == 1
    assert tools.calls == []


@pytest.mark.parametrize(
    ("action_id", "tool_id"),
    [
        ("", "read.demo"),
        ("finish\nforged-log", "read.demo"),
        ("finish\u202e", "read.demo"),
        ("e\u0301", "read.demo"),
        ("\ud800", "read.demo"),
        ("f" * 513, "read.demo"),
        ("finish", "read.demo\rforged"),
        ("finish", "\ud800"),
        ("finish", "read.demo" + "x" * 513),
    ],
)
def test_planner_identity_is_bounded_before_snapshot_or_history(
    action_id: str, tool_id: str
) -> None:
    class UntrustedPlanner(SingleStepPlanner):
        def plan(self, *, state: object, goal: object, actions: object) -> DeterministicPlan:
            self.calls += 1
            return DeterministicPlan(
                steps=(PlanStep(action_id=action_id, tool_id=tool_id),)
            )

    planner, tools = UntrustedPlanner(), RecordingTools()
    result = run_action(arguments={"safe": True}, planner=planner, tools=tools)
    assert result.error_code is DeterministicErrorCode.INVALID_PLAN
    assert result.error == "planner returned a malformed deterministic plan"
    assert result.planning_history == ()
    assert result.completed_actions == ()
    assert result.final_state == WorldState()
    assert planner.calls == 1
    assert tools.calls == []


def test_valid_planner_identity_still_completes_through_canonical_tools() -> None:
    planner, tools = SingleStepPlanner(), RecordingTools()
    result = run_action(arguments={"safe": True}, planner=planner, tools=tools)
    assert result.ok
    assert result.planning_history == (
        DeterministicPlan(steps=(PlanStep("finish", "read.demo"),)),
    )
    assert result.completed_actions == ("finish",)
    assert len(tools.calls) == 1
    assert tools.calls[0].approved is False


@pytest.mark.parametrize(
    "field_name",
    [
        "",
        " leading",
        "trailing ",
        "line\nbreak",
        "bidi\u202ereordered",
        "e\u0301",
        "x" * 513,
        "\ud800",
    ],
)
def test_nested_argument_field_name_cannot_spoof_evidence_or_tool_schema(
    field_name: str,
) -> None:
    planner, tools = SingleStepPlanner(), RecordingTools()
    with pytest.raises(ValueError, match="cannot be detached safely"):
        run_action(
            arguments={"outer": {field_name: "ordinary value"}},
            planner=planner,
            tools=tools,
        )
    assert planner.calls == 0
    assert tools.calls == []


def test_normal_nested_field_names_keep_plain_unicode_values() -> None:
    arguments = {"outer": {"user_text": "line\nbidi \u202e is text, not a field name"}}
    planner, tools = SingleStepPlanner(), RecordingTools()
    result = run_action(arguments=arguments, planner=planner, tools=tools)
    assert result.ok  # type: ignore[attr-defined]
    assert planner.calls == 1
    assert len(tools.calls) == 1
    assert tools.calls[0].arguments == arguments
    assert tools.calls[0].approved is False
