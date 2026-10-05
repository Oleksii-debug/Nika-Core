"""Reject malformed deterministic budgets before planning, journaling or tool effects."""

from __future__ import annotations

import asyncio
from decimal import Decimal

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

    def plan(self, *, state, goal, actions) -> DeterministicPlan:
        self.calls += 1
        return DeterministicPlan(steps=())


class GuardedJournal:
    def __init__(self) -> None:
        self.inspected = False

    def unresolved_operation_keys(self, *, task_id: str) -> tuple[str, ...]:
        self.inspected = True
        return ()


def _run(brain: DeterministicBrain, **overrides: object):
    arguments = {
        "run_id": "finite-budget-proof",
        "state": WorldState(),
        "goal": DeterministicGoal(),
        "actions": (),
    }
    arguments.update(overrides)
    return asyncio.run(brain.run(**arguments))


@pytest.mark.parametrize(
    "value",
    [None, True, False, 0, -1, 1.0, 1.5, "2", float("nan"), float("inf"), []],
)
def test_invalid_step_budgets_never_call_planner(value: object) -> None:
    planner = CountingPlanner()
    brain = DeterministicBrain(planner=planner, tools=ToolExecutor())
    with pytest.raises(ValueError, match="max_steps"):
        _run(brain, max_steps=value)
    assert planner.calls == 0


@pytest.mark.parametrize(
    "value",
    [None, True, False, -1, 0.5, 1.0, "1", float("nan"), float("inf"), []],
)
def test_invalid_replan_budgets_never_call_planner(value: object) -> None:
    planner = CountingPlanner()
    brain = DeterministicBrain(planner=planner, tools=ToolExecutor())
    with pytest.raises(ValueError, match="max_replans"):
        _run(brain, max_replans=value)
    assert planner.calls == 0


@pytest.mark.parametrize("field", ["planning_timeout_seconds", "observation_timeout_seconds"])
@pytest.mark.parametrize(
    "value",
    [
        None, True, False, "2", 0, -1, float("nan"), float("inf"),
        -float("inf"), 10**500, Decimal(1), object(),
    ],
)
def test_invalid_time_budgets_fail_before_planning_and_journal(
    field: str, value: object
) -> None:
    planner = CountingPlanner()
    journal = GuardedJournal()
    brain = DeterministicBrain(planner=planner, tools=ToolExecutor(), effect_journal=journal)
    with pytest.raises(ValueError, match=field):
        _run(brain, task_id="task-1", **{field: value})
    assert planner.calls == 0
    assert journal.inspected is False


@pytest.mark.parametrize("seconds", [1, 1.0, 0.5])
def test_finite_integral_and_float_timeouts_preserve_success(seconds: float) -> None:
    planner = CountingPlanner()
    brain = DeterministicBrain(planner=planner, tools=ToolExecutor())
    result = _run(
        brain,
        max_steps=1,
        max_replans=0,
        planning_timeout_seconds=seconds,
        observation_timeout_seconds=seconds,
    )
    assert result.ok
    assert planner.calls == 1


@pytest.mark.parametrize("field", ["run_id", "task_id"])
@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        0,
        "",
        " ",
        " leading",
        "trailing ",
        "line\nbreak",
        "\x00",
        "\x85",
        "safe\u200e-id",
        "safe\u202e-id",
        "safe\u2066-id",
        "safe\u2028-id",
        "safe\u2029-id",
        "Cafe\u0301",
        "\ud800",
        "x" * 513,
        "x" * 1_000_000,
        "😀" * 200,
    ],
)
def test_invalid_identity_never_reaches_planner_or_journal(
    field: str, value: object
) -> None:
    planner = CountingPlanner()
    journal = GuardedJournal()
    brain = DeterministicBrain(planner=planner, tools=ToolExecutor(), effect_journal=journal)
    arguments: dict[str, object] = {"task_id": "task-1"}
    arguments[field] = value
    with pytest.raises(ValueError, match=field):
        _run(brain, **arguments)
    assert planner.calls == 0
    assert journal.inspected is False


def test_canonical_ukrainian_identity_and_512_byte_id_remain_accepted() -> None:
    planner = CountingPlanner()
    journal = GuardedJournal()
    brain = DeterministicBrain(planner=planner, tools=ToolExecutor(), effect_journal=journal)
    result = _run(brain, run_id="Ніка: робота 1", task_id="x" * 512)
    assert result.ok
    assert planner.calls == 1
    assert journal.inspected is True


class OneStepPlanner:
    def plan(self, *, state, goal, actions) -> DeterministicPlan:
        return DeterministicPlan(steps=(PlanStep(action_id="advance"),))


def test_valid_ukrainian_run_and_task_complete_real_deterministic_step() -> None:
    journal = GuardedJournal()
    brain = DeterministicBrain(
        planner=OneStepPlanner(), tools=ToolExecutor(), effect_journal=journal
    )
    result = _run(
        brain,
        run_id="Ніка: перевірка",
        task_id="задача-1",
        goal=DeterministicGoal(required=frozenset({"готово"})),
        actions=(DeterministicAction(action_id="advance", adds=frozenset({"готово"})),),
        max_steps=1,
        max_replans=0,
        planning_timeout_seconds=2,
        observation_timeout_seconds=2,
    )
    assert result.ok
    assert result.completed_actions == ("advance",)
    assert result.final_state.facts == frozenset({"готово"})
    assert journal.inspected is True


def test_composed_unicode_run_identity_remains_accepted() -> None:
    planner = CountingPlanner()
    brain = DeterministicBrain(planner=planner, tools=ToolExecutor())
    result = _run(brain, run_id="Café")
    assert result.ok
    assert planner.calls == 1
