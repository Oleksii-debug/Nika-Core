from __future__ import annotations

import asyncio

import pytest

from nika_core.intelligence.brain import DeterministicBrain
from nika_core.intelligence.contracts import (
    DeterministicAction,
    DeterministicErrorCode,
    DeterministicGoal,
    DeterministicPlan,
    DeterministicPlanningError,
    InvalidDeterministicGoalError,
    PlanStep,
    WorldState,
)
from nika_core.tools import ToolExecutor, ToolRisk, ToolSpec


class _SingleActionPlanner:
    def plan(
        self,
        *,
        state: WorldState,
        goal: DeterministicGoal,
        actions: tuple[DeterministicAction, ...],
    ) -> DeterministicPlan:
        del state, goal
        action = actions[0]
        return DeterministicPlan(
            steps=(PlanStep(action_id=action.action_id, tool_id=action.tool_id),)
        )


class _NoopEffectJournal:
    def unresolved_operation_keys(self, *, task_id: str) -> tuple[str, ...]:
        del task_id
        return ()


@pytest.mark.parametrize(
    "planner_code",
    [DeterministicErrorCode.GOAL_UNREACHABLE, DeterministicErrorCode.NO_PLAN_FOUND],
)
def test_brain_classifies_planner_impossibility_as_no_valid_plan(
    planner_code: DeterministicErrorCode,
) -> None:
    class NoPlanPlanner:
        def plan(
            self,
            *,
            state: WorldState,
            goal: DeterministicGoal,
            actions: tuple[DeterministicAction, ...],
        ) -> DeterministicPlan:
            del state, goal, actions
            raise DeterministicPlanningError("goal has no valid deterministic plan", code=planner_code)

    result = asyncio.run(
        DeterministicBrain(planner=NoPlanPlanner(), tools=ToolExecutor()).run(
            run_id="no-plan",
            state=WorldState(),
            goal=DeterministicGoal(required=frozenset({"done"})),
            actions=(),
        )
    )

    assert not result.ok
    assert result.error_code is DeterministicErrorCode.NO_VALID_PLAN
    assert result.completed_actions == ()
    assert result.planning_history == ()


def test_brain_classifies_unknown_tool_as_missing_capability() -> None:
    action = DeterministicAction(
        action_id="use-missing-tool",
        adds=frozenset({"done"}),
        tool_id="missing.tool",
    )
    result = asyncio.run(
        DeterministicBrain(planner=_SingleActionPlanner(), tools=ToolExecutor()).run(
            run_id="missing-capability",
            state=WorldState(),
            goal=DeterministicGoal(required=frozenset({"done"})),
            actions=(action,),
        )
    )

    assert not result.ok
    assert result.error == "unknown tool"
    assert result.error_code is DeterministicErrorCode.MISSING_CAPABILITY
    assert result.completed_actions == ()


def test_brain_classifies_executor_approval_denial_as_policy_denied_capability() -> None:
    called = False

    async def publish(_arguments: dict[str, object]) -> object:
        nonlocal called
        called = True
        return {"published": True}

    tools = ToolExecutor()
    tools.register(
        ToolSpec(tool_id="publish", description="publish", risk=ToolRisk.HIGH_IMPACT),
        publish,
    )
    action = DeterministicAction(
        action_id="publish-result",
        adds=frozenset({"published"}),
        tool_id="publish",
    )
    result = asyncio.run(
        DeterministicBrain(
            planner=_SingleActionPlanner(),
            tools=tools,
            effect_journal=_NoopEffectJournal(),  # type: ignore[arg-type]
        ).run(
            run_id="policy-denied",
            task_id="task-policy-denied",
            state=WorldState(),
            goal=DeterministicGoal(required=frozenset({"published"})),
            actions=(action,),
        )
    )

    assert not result.ok
    assert result.error == "approval required"
    assert result.error_code is DeterministicErrorCode.POLICY_DENIED_CAPABILITY
    assert called is False


def test_brain_classifies_read_only_timeout_as_temporarily_unavailable_capability() -> None:
    async def unavailable(_arguments: dict[str, object]) -> object:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    tools = ToolExecutor()
    tools.register(
        ToolSpec(
            tool_id="remote.read",
            description="read",
            timeout_seconds=0.01,
        ),
        unavailable,
    )
    action = DeterministicAction(
        action_id="read-remote",
        adds=frozenset({"done"}),
        tool_id="remote.read",
    )
    result = asyncio.run(
        DeterministicBrain(planner=_SingleActionPlanner(), tools=tools).run(
            run_id="temporarily-unavailable",
            state=WorldState(),
            goal=DeterministicGoal(required=frozenset({"done"})),
            actions=(action,),
        )
    )

    assert not result.ok
    assert result.error == "tool timed out"
    assert result.error_code is DeterministicErrorCode.TEMPORARILY_UNAVAILABLE_CAPABILITY
    assert result.completed_actions == ()


def test_invalid_goal_has_canonical_invalid_goal_code() -> None:
    with pytest.raises(InvalidDeterministicGoalError) as raised:
        DeterministicGoal(
            required=frozenset({"same"}),
            forbidden=frozenset({"same"}),
        )

    assert raised.value.code is DeterministicErrorCode.INVALID_GOAL


def test_generic_tool_failure_is_not_misclassified_as_capability_gap() -> None:
    async def broken(_arguments: dict[str, object]) -> object:
        raise RuntimeError("synthetic adapter failure")

    tools = ToolExecutor()
    tools.register(ToolSpec(tool_id="broken.read", description="broken"), broken)
    action = DeterministicAction(
        action_id="broken-read",
        adds=frozenset({"done"}),
        tool_id="broken.read",
    )
    result = asyncio.run(
        DeterministicBrain(planner=_SingleActionPlanner(), tools=tools).run(
            run_id="generic-tool-failure",
            state=WorldState(),
            goal=DeterministicGoal(required=frozenset({"done"})),
            actions=(action,),
        )
    )

    assert not result.ok
    assert result.error == "tool failed"
    assert result.error_code is DeterministicErrorCode.TOOL_EXECUTION_FAILED
