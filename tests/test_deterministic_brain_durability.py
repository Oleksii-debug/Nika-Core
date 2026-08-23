from __future__ import annotations

import asyncio

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.intelligence.brain import DeterministicBrain
from nika_core.intelligence.contracts import (
    DeterministicAction,
    DeterministicErrorCode,
    DeterministicGoal,
    DeterministicPlan,
    DeterministicPlanningError,
    PlanStep,
    WorldState,
)
from nika_core.intelligence.runtime_effect_journal import RuntimeIdempotencyEffectJournal
from nika_core.intelligence.unified_planning_adapter import UnifiedPlanningAdapter
from nika_core.runtime.idempotency import IdempotencyLedger
from nika_core.tools import ToolExecutor, ToolRisk, ToolSpec


def test_planner_action_id_cannot_manufacture_high_impact_approval(tmp_path) -> None:
    called = False

    async def publish(_arguments: dict[str, object]) -> object:
        nonlocal called
        called = True
        return "published"

    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    tools = ToolExecutor()
    tools.register(
        ToolSpec(tool_id="publish", description="publish", risk=ToolRisk.HIGH_IMPACT),
        publish,
    )
    brain = DeterministicBrain(
        planner=UnifiedPlanningAdapter(),
        tools=tools,
        effect_journal=RuntimeIdempotencyEffectJournal(IdempotencyLedger(store)),
    )
    action = DeterministicAction(
        action_id="publish-result",
        adds=frozenset({"published"}),
        tool_id="publish",
    )

    result = asyncio.run(
        brain.run(
            run_id="planner-approval-bypass",
            task_id="task-planner-approval-bypass",
            execution_id="execution-planner-approval-bypass",
            state=WorldState(),
            goal=DeterministicGoal(required=frozenset({"published"})),
            actions=(action,),
            approved_action_ids=frozenset({"publish-result"}),
        )
    )

    assert not result.ok
    assert result.error == "approval required"
    assert result.error_code is DeterministicErrorCode.TOOL_EXECUTION_FAILED
    assert called is False
    assert IdempotencyLedger(store).list_for_task("task-planner-approval-bypass") == ()


def test_changed_state_replans_without_repeating_completed_effect() -> None:
    called: list[str] = []

    async def prepare(_arguments: dict[str, object]) -> object:
        called.append("prepare")
        return "prepared"

    async def finish(_arguments: dict[str, object]) -> object:
        called.append("finish")
        return "finished"

    async def recover(_arguments: dict[str, object]) -> object:
        called.append("recover")
        return "recovered"

    class Observer:
        def __init__(self) -> None:
            self._states = [
                WorldState(frozenset({"ready"})),
                WorldState(frozenset({"ready", "changed"})),
                WorldState(frozenset({"ready", "changed"})),
                WorldState(frozenset({"ready", "changed", "done"})),
            ]
            self._last = self._states[-1]

        async def observe(self) -> WorldState:
            if self._states:
                self._last = self._states.pop(0)
            return self._last

    tools = ToolExecutor()
    tools.register(ToolSpec(tool_id="prepare", description="prepare"), prepare)
    tools.register(ToolSpec(tool_id="finish", description="finish"), finish)
    tools.register(ToolSpec(tool_id="recover", description="recover"), recover)
    brain = DeterministicBrain(
        planner=UnifiedPlanningAdapter(),
        tools=tools,
    )
    actions = (
        DeterministicAction(
            action_id="prepare",
            requires=frozenset({"ready"}),
            adds=frozenset({"prepared"}),
            tool_id="prepare",
        ),
        DeterministicAction(
            action_id="finish",
            requires=frozenset({"prepared"}),
            adds=frozenset({"done"}),
            tool_id="finish",
        ),
        DeterministicAction(
            action_id="recover",
            requires=frozenset({"changed"}),
            adds=frozenset({"done"}),
            tool_id="recover",
        ),
    )

    result = asyncio.run(
        brain.run(
            run_id="changed-state",
            state=WorldState(frozenset({"ready"})),
            goal=DeterministicGoal(required=frozenset({"done"})),
            actions=actions,
            state_observer=Observer(),
        )
    )

    assert result.ok
    assert result.replans == 1
    assert len(result.planning_history) == 2
    assert result.completed_actions == ("prepare", "recover")
    assert called == ["prepare", "recover"]


def test_restart_resume_does_not_repeat_completed_tool_effect() -> None:
    calls = {"download": 0, "index": 0}
    fail_index = True

    async def download(_arguments: dict[str, object]) -> object:
        calls["download"] += 1
        return "downloaded"

    async def index(_arguments: dict[str, object]) -> object:
        nonlocal fail_index
        calls["index"] += 1
        if fail_index:
            raise RuntimeError("simulated adapter failure")
        return "indexed"

    tools = ToolExecutor()
    tools.register(ToolSpec(tool_id="download", description="download"), download)
    tools.register(ToolSpec(tool_id="index", description="index"), index)
    actions = (
        DeterministicAction(
            action_id="download",
            requires=frozenset({"source-ready"}),
            adds=frozenset({"downloaded"}),
            tool_id="download",
        ),
        DeterministicAction(
            action_id="index",
            requires=frozenset({"downloaded"}),
            adds=frozenset({"indexed"}),
            tool_id="index",
        ),
    )
    goal = DeterministicGoal(required=frozenset({"indexed"}))

    first = asyncio.run(
        DeterministicBrain(
            planner=UnifiedPlanningAdapter(),
            tools=tools,
        ).run(
            run_id="restart-before",
            state=WorldState(frozenset({"source-ready"})),
            goal=goal,
            actions=actions,
        )
    )
    assert not first.ok
    assert first.completed_actions == ("download",)
    assert calls == {"download": 1, "index": 1}

    fail_index = False
    second = asyncio.run(
        DeterministicBrain(
            planner=UnifiedPlanningAdapter(),
            tools=tools,
        ).run(
            run_id="restart-after",
            state=first.final_state,
            goal=goal,
            actions=actions,
            previously_completed_action_ids=first.completed_actions,
        )
    )

    assert second.ok
    assert second.completed_actions == ("download", "index")
    assert calls == {"download": 1, "index": 2}


def test_entire_plan_is_validated_before_first_tool_action() -> None:
    called = False

    class MaliciousPlanner:
        def plan(
            self,
            *,
            state: WorldState,
            goal: DeterministicGoal,
            actions: tuple[DeterministicAction, ...],
        ) -> DeterministicPlan:
            del state, goal, actions
            return DeterministicPlan(
                steps=(
                    PlanStep(action_id="publish", tool_id="publish"),
                    PlanStep(action_id="not-registered"),
                )
            )

    async def publish(_arguments: dict[str, object]) -> object:
        nonlocal called
        called = True
        return "published"

    tools = ToolExecutor()
    tools.register(
        ToolSpec(tool_id="publish", description="publish", risk=ToolRisk.HIGH_IMPACT),
        publish,
    )
    brain = DeterministicBrain(planner=MaliciousPlanner(), tools=tools)
    action = DeterministicAction(
        action_id="publish",
        adds=frozenset({"published"}),
        tool_id="publish",
    )

    result = asyncio.run(
        brain.run(
            run_id="validate-first",
            state=WorldState(),
            goal=DeterministicGoal(required=frozenset({"published"})),
            actions=(action,),
        )
    )

    assert result.error_code is DeterministicErrorCode.INVALID_PLAN
    assert called is False


@pytest.mark.parametrize(
    ("steps", "expected"),
    [
        (
            (PlanStep(action_id="advance", tool_id="wrong-tool"),),
            "planned tool identity does not match action",
        ),
        (
            (
                PlanStep(action_id="advance", tool_id="advance"),
                PlanStep(action_id="advance", tool_id="advance"),
            ),
            "plan repeats a completed deterministic action",
        ),
        (
            (PlanStep(action_id="noop"),),
            "planned action has no new deterministic effect",
        ),
    ],
)
def test_adversarial_plans_fail_closed(
    steps: tuple[PlanStep, ...],
    expected: str,
) -> None:
    class FixedPlanner:
        def plan(
            self,
            *,
            state: WorldState,
            goal: DeterministicGoal,
            actions: tuple[DeterministicAction, ...],
        ) -> DeterministicPlan:
            del state, goal, actions
            return DeterministicPlan(steps=steps)

    action = (
        DeterministicAction(action_id="noop")
        if steps[0].action_id == "noop"
        else DeterministicAction(
            action_id="advance",
            adds=frozenset({"done"}),
            tool_id="advance",
        )
    )
    brain = DeterministicBrain(planner=FixedPlanner(), tools=ToolExecutor())

    result = asyncio.run(
        brain.run(
            run_id="adversarial-plan",
            state=WorldState(),
            goal=DeterministicGoal(required=frozenset({"done"})),
            actions=(action,),
        )
    )

    assert result.error_code is DeterministicErrorCode.INVALID_PLAN
    assert expected in (result.error or "")


def test_thousand_step_plan_is_rejected_before_any_tool() -> None:
    called = False

    class HugePlanner:
        def plan(
            self,
            *,
            state: WorldState,
            goal: DeterministicGoal,
            actions: tuple[DeterministicAction, ...],
        ) -> DeterministicPlan:
            del state, goal, actions
            return DeterministicPlan(
                steps=tuple(
                    PlanStep(action_id="advance", tool_id="advance")
                    for _ in range(1000)
                )
            )

    async def advance(_arguments: dict[str, object]) -> object:
        nonlocal called
        called = True
        return "advanced"

    tools = ToolExecutor()
    tools.register(ToolSpec(tool_id="advance", description="advance"), advance)
    brain = DeterministicBrain(planner=HugePlanner(), tools=tools)
    action = DeterministicAction(
        action_id="advance",
        adds=frozenset({"done"}),
        tool_id="advance",
    )

    result = asyncio.run(
        brain.run(
            run_id="scale-budget",
            state=WorldState(),
            goal=DeterministicGoal(required=frozenset({"done"})),
            actions=(action,),
            max_steps=100,
        )
    )

    assert result.error_code is DeterministicErrorCode.PLAN_TOO_LONG
    assert called is False


def test_changed_state_replanning_is_bounded() -> None:
    class FixedPlanner:
        def plan(
            self,
            *,
            state: WorldState,
            goal: DeterministicGoal,
            actions: tuple[DeterministicAction, ...],
        ) -> DeterministicPlan:
            del state, goal, actions
            return DeterministicPlan(steps=(PlanStep(action_id="advance"),))

    class FlappingObserver:
        def __init__(self) -> None:
            self._toggle = False

        async def observe(self) -> WorldState:
            self._toggle = not self._toggle
            fact = "left" if self._toggle else "right"
            return WorldState(frozenset({fact}))

    brain = DeterministicBrain(planner=FixedPlanner(), tools=ToolExecutor())
    action = DeterministicAction(action_id="advance", adds=frozenset({"done"}))

    result = asyncio.run(
        brain.run(
            run_id="bounded-replan",
            state=WorldState(),
            goal=DeterministicGoal(required=frozenset({"done"})),
            actions=(action,),
            state_observer=FlappingObserver(),
            max_replans=2,
        )
    )

    assert result.error_code is DeterministicErrorCode.REPLAN_LIMIT
    assert result.replans == 3
    assert result.completed_actions == ()


def test_resume_rejects_invalid_completed_action_identity() -> None:
    brain = DeterministicBrain(
        planner=UnifiedPlanningAdapter(),
        tools=ToolExecutor(),
    )
    action = DeterministicAction(action_id="known", adds=frozenset({"done"}))

    with pytest.raises(ValueError, match="duplicate previously completed"):
        asyncio.run(
            brain.run(
                run_id="duplicate-resume",
                state=WorldState(),
                goal=DeterministicGoal(required=frozenset({"done"})),
                actions=(action,),
                previously_completed_action_ids=("known", "known"),
            )
        )

    with pytest.raises(ValueError, match="previously completed deterministic action"):
        asyncio.run(
            brain.run(
                run_id="unknown-resume",
                state=WorldState(),
                goal=DeterministicGoal(required=frozenset({"done"})),
                actions=(action,),
                previously_completed_action_ids=("missing",),
            )
        )


def test_unified_planning_impossible_goal_has_typed_error() -> None:
    planner = UnifiedPlanningAdapter()

    with pytest.raises(DeterministicPlanningError) as raised:
        planner.plan(
            state=WorldState(),
            goal=DeterministicGoal(required=frozenset({"unreachable"})),
            actions=(),
        )

    assert raised.value.code in {
        DeterministicErrorCode.GOAL_UNREACHABLE,
        DeterministicErrorCode.NO_PLAN_FOUND,
    }
