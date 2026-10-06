from __future__ import annotations

import asyncio
import time

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.intelligence.brain import DeterministicBrain
from nika_core.intelligence.contracts import (
    DeterministicAction,
    DeterministicGoal,
    DeterministicPlan,
    DeterministicPlanningError,
    PlanStep,
    WorldState,
)
from nika_core.intelligence.runtime_effect_journal import RuntimeIdempotencyEffectJournal
from nika_core.intelligence.unified_planning_adapter import UnifiedPlanningAdapter
from nika_core.kernel.task_queue import TaskQueue
from nika_core.runtime.idempotency import IdempotencyLedger
from nika_core.tools import ToolExecutor, ToolRisk, ToolSpec


def test_deterministic_brain_plans_and_executes_without_model_gateway() -> None:
    called: list[str] = []

    async def fetch(_arguments: dict[str, object]) -> object:
        called.append("fetch")
        return {"pages": 4}

    async def filter_pages(_arguments: dict[str, object]) -> object:
        called.append("filter")
        return {"candidates": 2}

    tools = ToolExecutor()
    tools.register(ToolSpec(tool_id="research.fetch", description="fetch"), fetch)
    tools.register(ToolSpec(tool_id="research.filter", description="filter"), filter_pages)
    brain = DeterministicBrain(
        planner=UnifiedPlanningAdapter(),
        tools=tools,
    )
    actions = (
        DeterministicAction(
            action_id="fetch-pages",
            requires=frozenset({"source-configured"}),
            adds=frozenset({"pages-fetched"}),
            tool_id="research.fetch",
        ),
        DeterministicAction(
            action_id="filter-pages",
            requires=frozenset({"pages-fetched"}),
            adds=frozenset({"candidates-ready"}),
            tool_id="research.filter",
        ),
    )

    result = asyncio.run(
        brain.run(
            run_id="research-1",
            state=WorldState(frozenset({"source-configured"})),
            goal=DeterministicGoal(required=frozenset({"candidates-ready"})),
            actions=actions,
        )
    )

    assert result.ok
    assert result.completed_actions == ("fetch-pages", "filter-pages")
    assert called == ["fetch", "filter"]
    assert "candidates-ready" in result.final_state.facts


def test_deterministic_planner_fails_cleanly_for_impossible_goal() -> None:
    planner = UnifiedPlanningAdapter()

    with pytest.raises(DeterministicPlanningError):
        planner.plan(
            state=WorldState(),
            goal=DeterministicGoal(required=frozenset({"unreachable"})),
            actions=(),
        )


def test_replanning_from_changed_state_skips_already_completed_work() -> None:
    planner = UnifiedPlanningAdapter()
    actions = (
        DeterministicAction(
            action_id="fetch-pages",
            requires=frozenset({"source-configured"}),
            adds=frozenset({"pages-fetched"}),
        ),
        DeterministicAction(
            action_id="filter-pages",
            requires=frozenset({"pages-fetched"}),
            adds=frozenset({"candidates-ready"}),
        ),
    )

    plan = planner.plan(
        state=WorldState(frozenset({"source-configured", "pages-fetched"})),
        goal=DeterministicGoal(required=frozenset({"candidates-ready"})),
        actions=actions,
    )

    assert tuple(step.action_id for step in plan.steps) == ("filter-pages",)


def test_deterministic_brain_cannot_bypass_high_impact_tool_approval(tmp_path) -> None:
    called = False

    async def publish(_arguments: dict[str, object]) -> object:
        nonlocal called
        called = True
        return "published"

    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    task = TaskQueue(store).create(workspace_id="proof", agent_id="deterministic")
    tools = ToolExecutor()
    tools.register(
        ToolSpec(
            tool_id="publish",
            description="publish",
            risk=ToolRisk.HIGH_IMPACT,
        ),
        publish,
    )
    brain = DeterministicBrain(
        planner=UnifiedPlanningAdapter(),
        tools=tools,
        effect_journal=RuntimeIdempotencyEffectJournal(IdempotencyLedger(store)),
    )
    action = DeterministicAction(
        action_id="publish-result",
        requires=frozenset({"draft-ready"}),
        adds=frozenset({"published"}),
        tool_id="publish",
    )

    result = asyncio.run(
        brain.run(
            run_id="approval-proof",
            task_id=task.task_id,
            state=WorldState(frozenset({"draft-ready"})),
            goal=DeterministicGoal(required=frozenset({"published"})),
            actions=(action,),
        )
    )

    assert not result.ok
    assert result.error == "approval required"
    assert called is False
    assert "published" not in result.final_state.facts
    assert IdempotencyLedger(store).list_for_task(task.task_id) == ()


def test_deterministic_brain_rejects_plan_over_step_budget_before_execution() -> None:
    called = False

    class OversizedPlanner:
        def plan(
            self,
            *,
            state: WorldState,
            goal: DeterministicGoal,
            actions: tuple[DeterministicAction, ...],
        ) -> DeterministicPlan:
            del state, goal, actions
            return DeterministicPlan(
                steps=(PlanStep(action_id="advance"), PlanStep(action_id="advance"))
            )

    async def advance(_arguments: dict[str, object]) -> object:
        nonlocal called
        called = True
        return "advanced"

    tools = ToolExecutor()
    tools.register(ToolSpec(tool_id="advance", description="advance"), advance)
    brain = DeterministicBrain(planner=OversizedPlanner(), tools=tools)
    action = DeterministicAction(
        action_id="advance",
        adds=frozenset({"done"}),
        tool_id="advance",
    )

    result = asyncio.run(
        brain.run(
            run_id="bounded-plan",
            state=WorldState(),
            goal=DeterministicGoal(required=frozenset({"done"})),
            actions=(action,),
            max_steps=1,
        )
    )

    assert not result.ok
    assert result.error == "plan exceeds max_steps budget: 2 > 1"
    assert result.completed_actions == ()
    assert called is False


def test_deterministic_brain_times_out_slow_planner() -> None:
    class SlowPlanner:
        def plan(
            self,
            *,
            state: WorldState,
            goal: DeterministicGoal,
            actions: tuple[DeterministicAction, ...],
        ) -> DeterministicPlan:
            del state, goal, actions
            time.sleep(0.05)
            return DeterministicPlan(steps=())

    brain = DeterministicBrain(planner=SlowPlanner(), tools=ToolExecutor())

    with pytest.raises(DeterministicPlanningError, match="timed out"):
        asyncio.run(
            brain.run(
                run_id="planner-timeout",
                state=WorldState(),
                goal=DeterministicGoal(),
                actions=(),
                planning_timeout_seconds=0.01,
            )
        )


def test_deterministic_brain_rejects_invalid_budget_values() -> None:
    brain = DeterministicBrain(
        planner=UnifiedPlanningAdapter(),
        tools=ToolExecutor(),
    )

    with pytest.raises(ValueError, match="max_steps"):
        asyncio.run(
            brain.run(
                run_id="invalid-budget",
                state=WorldState(),
                goal=DeterministicGoal(),
                actions=(),
                max_steps=0,
            )
        )

    with pytest.raises(ValueError, match="planning_timeout_seconds"):
        asyncio.run(
            brain.run(
                run_id="invalid-timeout",
                state=WorldState(),
                goal=DeterministicGoal(),
                actions=(),
                planning_timeout_seconds=0,
            )
        )
