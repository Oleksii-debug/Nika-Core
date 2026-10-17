"""Plan 3 §1: catalog-wide budgets precede planner and durable effects."""

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
        raise AssertionError("invalid catalog must not inspect durable effects")


def run_catalog(
    actions: tuple[DeterministicAction, ...],
    *,
    with_journal: bool,
) -> tuple[object, CountingPlanner, DenyJournal]:
    planner = CountingPlanner()
    journal = DenyJournal()
    brain = DeterministicBrain(
        planner=planner,
        tools=ToolExecutor(),
        effect_journal=journal if with_journal else None,  # type: ignore[arg-type]
    )
    result = brain.run(
        run_id="aggregate-budget",
        task_id="durable-task" if with_journal else None,
        state=WorldState(),
        goal=DeterministicGoal(required=frozenset({"done"})),
        actions=actions,
    )
    return result, planner, journal


@pytest.mark.parametrize("kind", ["cumulative_bytes", "cumulative_nodes"])
def test_catalog_budget_rejects_aggregate_resource_exhaustion_before_effects(
    kind: str,
) -> None:
    if kind == "cumulative_bytes":
        # Each action is independently <= 256 KiB; 17 exceed the 4 MiB
        # whole-catalog budget previously missing from the admission gate.
        actions = tuple(
            DeterministicAction(
                action_id=f"step-{index}",
                adds=frozenset({f"result-{index}"}),
                arguments={"payload": "x" * (256 * 1024 - len("payload"))},
            )
            for index in range(17)
        )
    else:
        # Each action stays below the individual 10,000-node cap.
        # Eleven such actions exceed the catalog-wide 100,000-node cap.
        actions = tuple(
            DeterministicAction(
                action_id=f"step-{index}",
                adds=frozenset({f"result-{index}"}),
                arguments={"numbers": [0] * 9_800},
            )
            for index in range(11)
        )

    result, planner, journal = run_catalog(actions, with_journal=True)
    with pytest.raises(ValueError, match="cannot be detached safely"):
        asyncio.run(result)
    assert planner.calls == 0
    assert journal.reads == 0



def test_aggregate_fact_slots_are_bounded_before_journal_or_planner() -> None:
    # Per-action fact sets are valid, but 11 times 10k slots is not a
    # bounded whole-catalog planning input, even when entries are shared.
    shared = frozenset(f"fact-{index}" for index in range(10_000))
    actions = tuple(
        DeterministicAction(
            action_id=f"step-{index}",
            requires=shared,
            adds=frozenset({f"result-{index}"}),
        )
        for index in range(11)
    )
    result, planner, journal = run_catalog(actions, with_journal=True)
    with pytest.raises(ValueError, match="cannot be detached safely"):
        asyncio.run(result)
    assert planner.calls == 0
    assert journal.reads == 0


def test_small_multi_action_catalog_still_executes() -> None:
    actions = (
        DeterministicAction(
            action_id="finish",
            adds=frozenset({"done"}),
            arguments={"payload": "x" * 65_536},
        ),
        DeterministicAction(
            action_id="unused",
            adds=frozenset({"unused"}),
            arguments={"payload": "y" * 65_536},
        ),
    )
    result, planner, journal = run_catalog(actions, with_journal=False)
    finished = asyncio.run(result)
    assert finished.ok
    assert finished.completed_actions == ("finish",)
    assert finished.final_state.facts == frozenset({"done"})
    assert planner.calls == 1
    assert journal.reads == 0
