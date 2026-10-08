"""Plan 3 §1: budget envelopes are rejected before planner or effect access."""

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
from nika_core.tools import ToolExecutor


class CountingPlanner:
    def __init__(self) -> None:
        self.calls = 0

    def plan(self, *, state: object, goal: object, actions: object) -> DeterministicPlan:
        self.calls += 1
        return DeterministicPlan(steps=(PlanStep("finish"),))


class DenyJournal:
    def __init__(self) -> None:
        self.reads = 0

    def unresolved_operation_keys(self, *, task_id: str) -> tuple[str, ...]:
        self.reads += 1
        raise AssertionError("journal must not run for an invalid budget")


@pytest.mark.parametrize(
    "override",
    [
        {"max_steps": 10_001},
        {"max_steps": 10**100},
        {"max_replans": 10_001},
        {"max_replans": 10**100},
        {"planning_timeout_seconds": 86_401},
        {"observation_timeout_seconds": 86_400.01},
    ],
)
def test_excessive_budget_rejected_before_effects(override: dict[str, object]) -> None:
    planner = CountingPlanner()
    journal = DenyJournal()
    with pytest.raises(ValueError):
        asyncio.run(
            DeterministicBrain(
                planner=planner,
                tools=ToolExecutor(),
                effect_journal=journal,  # type: ignore[arg-type]
            ).run(
                run_id="oversized-runtime-budget",
                task_id="scoped-task",
                state=WorldState(),
                goal=DeterministicGoal(required=frozenset({"done"})),
                actions=(DeterministicAction("finish", adds=frozenset({"done"})),),
                **override,
            )
        )
    assert planner.calls == 0
    assert journal.reads == 0


def test_documented_budget_upper_bounds_are_admitted() -> None:
    planner = CountingPlanner()
    result = asyncio.run(
        DeterministicBrain(planner=planner, tools=ToolExecutor()).run(
            run_id="bounded-finished-goal",
            state=WorldState(facts=frozenset({"done"})),
            goal=DeterministicGoal(required=frozenset({"done"})),
            actions=(),
            max_steps=10_000,
            max_replans=10_000,
            planning_timeout_seconds=86_400,
            observation_timeout_seconds=86_400.0,
        )
    )
    assert result.ok
    assert planner.calls == 0
