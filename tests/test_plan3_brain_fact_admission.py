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
from nika_core.tools import ToolExecutor


class CountingPlanner:
    def __init__(self) -> None:
        self.calls = 0

    def plan(self, *, state: object, goal: object, actions: object) -> DeterministicPlan:
        self.calls += 1
        return DeterministicPlan(steps=(PlanStep("finish"),))


class HostileObserver:
    def __init__(self, fact: str) -> None:
        self.fact = fact
        self.calls = 0

    async def observe(self) -> WorldState:
        self.calls += 1
        return WorldState(facts=frozenset({self.fact}))


BAD_FACTS = (
    "bad\nline",
    "control\u202ehidden",
    "non-NFC-e\u0301",
    "\ud800",
    "x" * 513,
    "edge ",
)


@pytest.mark.parametrize("bad_fact", BAD_FACTS)
@pytest.mark.parametrize("carrier", ("state", "goal", "action"))
def test_noncanonical_fact_rejected_before_planner_and_effects(
    bad_fact: str, carrier: str
) -> None:
    planner = CountingPlanner()
    state = WorldState(facts=frozenset({bad_fact})) if carrier == "state" else WorldState()
    goal = (
        DeterministicGoal(required=frozenset({bad_fact}))
        if carrier == "goal"
        else DeterministicGoal(required=frozenset({"done"}))
    )
    action = DeterministicAction(
        action_id="finish",
        adds=frozenset({bad_fact}) if carrier == "action" else frozenset({"done"}),
    )
    with pytest.raises(ValueError, match="deterministic run inputs cannot be detached safely"):
        asyncio.run(
            DeterministicBrain(planner=planner, tools=ToolExecutor()).run(
                run_id="bad-facts",
                state=state,
                goal=goal,
                actions=(action,),
            )
        )
    assert planner.calls == 0


def test_oversized_fact_set_rejected_before_planning() -> None:
    planner = CountingPlanner()
    state = WorldState(facts=frozenset(f"fact-{i}" for i in range(10001)))
    with pytest.raises(ValueError, match="deterministic run inputs cannot be detached safely"):
        asyncio.run(
            DeterministicBrain(planner=planner, tools=ToolExecutor()).run(
                run_id="oversized-facts",
                state=state,
                goal=DeterministicGoal(required=frozenset({"done"})),
                actions=(DeterministicAction("finish", adds=frozenset({"done"})),),
            )
        )
    assert planner.calls == 0


@pytest.mark.parametrize("bad_fact", BAD_FACTS)
def test_untrusted_observer_fact_fails_closed_without_planning(
    bad_fact: str,
) -> None:
    planner = CountingPlanner()
    observer = HostileObserver(bad_fact)
    result = asyncio.run(
        DeterministicBrain(planner=planner, tools=ToolExecutor()).run(
            run_id="observer-bad-facts",
            state=WorldState(facts=frozenset({"done"})),
            goal=DeterministicGoal(required=frozenset({"done"})),
            actions=(),
            state_observer=observer,
        )
    )
    assert not result.ok
    assert result.error_code == DeterministicErrorCode.STATE_OBSERVATION_FAILED
    assert result.completed_actions == ()
    assert result.planning_history == ()
    assert planner.calls == 0
    assert observer.calls == 1


def test_valid_unicode_facts_preserve_clean_execution_path() -> None:
    planner = CountingPlanner()
    result = asyncio.run(
        DeterministicBrain(planner=planner, tools=ToolExecutor()).run(
            run_id="valid-unicode-facts",
            state=WorldState(facts=frozenset({"джерело"})),
            goal=DeterministicGoal(required=frozenset({"готово"})),
            actions=(DeterministicAction("finish", adds=frozenset({"готово"})),),
        )
    )
    assert result.ok
    assert result.completed_actions == ("finish",)
    assert "готово" in result.final_state.facts
    assert planner.calls == 1
