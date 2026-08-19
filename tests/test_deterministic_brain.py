from __future__ import annotations

import asyncio

import pytest

from nika_core.intelligence.brain import DeterministicBrain
from nika_core.intelligence.contracts import (
    DeterministicAction,
    DeterministicGoal,
    DeterministicPlanningError,
    WorldState,
)
from nika_core.intelligence.unified_planning_adapter import UnifiedPlanningAdapter
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
        planner=UnifiedPlanningAdapter(engine_name="pyperplan"),
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
    planner = UnifiedPlanningAdapter(engine_name="pyperplan")

    with pytest.raises(DeterministicPlanningError):
        planner.plan(
            state=WorldState(),
            goal=DeterministicGoal(required=frozenset({"unreachable"})),
            actions=(),
        )


def test_replanning_from_changed_state_skips_already_completed_work() -> None:
    planner = UnifiedPlanningAdapter(engine_name="pyperplan")
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


def test_deterministic_brain_cannot_bypass_high_impact_tool_approval() -> None:
    called = False

    async def publish(_arguments: dict[str, object]) -> object:
        nonlocal called
        called = True
        return "published"

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
        planner=UnifiedPlanningAdapter(engine_name="pyperplan"),
        tools=tools,
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
            state=WorldState(frozenset({"draft-ready"})),
            goal=DeterministicGoal(required=frozenset({"published"})),
            actions=(action,),
        )
    )

    assert not result.ok
    assert result.error == "approval required"
    assert called is False
    assert "published" not in result.final_state.facts
