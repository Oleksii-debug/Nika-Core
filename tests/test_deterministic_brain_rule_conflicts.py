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


class _FirstActionPlanner:
    def __init__(self) -> None:
        self.calls = 0

    def plan(
        self,
        *,
        state: WorldState,
        goal: DeterministicGoal,
        actions: tuple[DeterministicAction, ...],
    ) -> DeterministicPlan:
        del state, goal
        self.calls += 1
        action = actions[0]
        return DeterministicPlan(
            steps=(PlanStep(action_id=action.action_id, tool_id=action.tool_id),)
        )


class _FixedPlanner:
    def __init__(self, *action_ids: str) -> None:
        self._action_ids = action_ids
        self.calls = 0

    def plan(
        self,
        *,
        state: WorldState,
        goal: DeterministicGoal,
        actions: tuple[DeterministicAction, ...],
    ) -> DeterministicPlan:
        del state, goal
        self.calls += 1
        action_map = {action.action_id: action for action in actions}
        return DeterministicPlan(
            steps=tuple(
                PlanStep(action_id=action_id, tool_id=action_map[action_id].tool_id)
                for action_id in self._action_ids
            )
        )


def test_current_rule_conflict_is_insertion_order_independent_and_pre_effect() -> None:
    invoked: list[str] = []

    async def effect(arguments: dict[str, object]) -> object:
        invoked.append(str(arguments["source"]))
        return "should-not-run"

    preserve = DeterministicAction(
        action_id="allow-finish",
        requires=frozenset({"ready"}),
        adds=frozenset({"done", "permission"}),
        tool_id="effect",
        arguments={"source": "allow"},
    )
    revoke = DeterministicAction(
        action_id="deny-finish",
        requires=frozenset({"ready"}),
        adds=frozenset({"done"}),
        removes=frozenset({"permission"}),
        tool_id="effect",
        arguments={"source": "deny"},
    )
    state = WorldState(frozenset({"ready", "permission"}))
    goal = DeterministicGoal(required=frozenset({"done"}))

    outcomes: list[tuple[object, ...]] = []
    for actions in ((preserve, revoke), (revoke, preserve)):
        planner = _FirstActionPlanner()
        tools = ToolExecutor()
        tools.register(ToolSpec(tool_id="effect", description="effect"), effect)
        result = asyncio.run(
            DeterministicBrain(planner=planner, tools=tools).run(
                run_id="rule-conflict-order",
                state=state,
                goal=goal,
                actions=actions,
            )
        )

        outcomes.append(
            (
                result.error_code,
                result.error,
                result.completed_actions,
                result.final_state,
                result.planning_history,
            )
        )
        assert planner.calls == 0

    assert outcomes[0] == outcomes[1]
    assert outcomes[0][0] is DeterministicErrorCode.RULE_CONFLICT
    assert outcomes[0][1] == (
        "deterministic rule conflict: "
        "fact=permission; add_action=allow-finish; remove_action=deny-finish"
    )
    assert outcomes[0][2] == ()
    assert outcomes[0][3] == state
    assert outcomes[0][4] == ()
    assert invoked == []


def test_conflict_exposed_by_later_plan_state_blocks_every_effect() -> None:
    invoked: list[str] = []

    async def effect(arguments: dict[str, object]) -> object:
        invoked.append(str(arguments["source"]))
        return "should-not-run"

    actions = (
        DeterministicAction(
            action_id="open-window",
            requires=frozenset({"ready"}),
            adds=frozenset({"window-open"}),
            tool_id="effect",
            arguments={"source": "open"},
        ),
        DeterministicAction(
            action_id="allow-finish",
            requires=frozenset({"window-open"}),
            adds=frozenset({"done", "permission"}),
            tool_id="effect",
            arguments={"source": "allow"},
        ),
        DeterministicAction(
            action_id="deny-finish",
            requires=frozenset({"window-open"}),
            adds=frozenset({"done"}),
            removes=frozenset({"permission"}),
            tool_id="effect",
            arguments={"source": "deny"},
        ),
    )
    planner = _FixedPlanner("open-window", "allow-finish")
    tools = ToolExecutor()
    tools.register(ToolSpec(tool_id="effect", description="effect"), effect)
    state = WorldState(frozenset({"ready", "permission"}))

    result = asyncio.run(
        DeterministicBrain(planner=planner, tools=tools).run(
            run_id="rule-conflict-later-state",
            state=state,
            goal=DeterministicGoal(required=frozenset({"done"})),
            actions=actions,
        )
    )

    assert planner.calls == 1
    assert result.error_code is DeterministicErrorCode.RULE_CONFLICT
    assert result.error == (
        "deterministic rule conflict: "
        "fact=permission; add_action=allow-finish; remove_action=deny-finish"
    )
    assert result.completed_actions == ()
    assert result.final_state == state
    assert invoked == []


def test_non_conflicting_plan_keeps_existing_execution_semantics() -> None:
    invoked: list[str] = []

    async def effect(arguments: dict[str, object]) -> object:
        invoked.append(str(arguments["source"]))
        return "ok"

    actions = (
        DeterministicAction(
            action_id="open-window",
            requires=frozenset({"ready"}),
            adds=frozenset({"window-open"}),
            tool_id="effect",
            arguments={"source": "open"},
        ),
        DeterministicAction(
            action_id="allow-finish",
            requires=frozenset({"window-open"}),
            adds=frozenset({"done", "permission"}),
            tool_id="effect",
            arguments={"source": "allow"},
        ),
    )
    planner = _FixedPlanner("open-window", "allow-finish")
    tools = ToolExecutor()
    tools.register(ToolSpec(tool_id="effect", description="effect"), effect)

    result = asyncio.run(
        DeterministicBrain(planner=planner, tools=tools).run(
            run_id="rule-conflict-control",
            state=WorldState(frozenset({"ready", "permission"})),
            goal=DeterministicGoal(required=frozenset({"done"})),
            actions=actions,
        )
    )

    assert result.ok
    assert result.completed_actions == ("open-window", "allow-finish")
    assert result.error_code is None
    assert invoked == ["open", "allow"]
