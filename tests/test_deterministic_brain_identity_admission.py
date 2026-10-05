"""Malformed deterministic caller identities fail before planner and durable effects."""

from __future__ import annotations

import asyncio

import pytest

from nika_core.intelligence.brain import DeterministicBrain
from nika_core.intelligence.contracts import DeterministicGoal, DeterministicPlan, WorldState
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


class HostileStr(str):
    def strip(self, *args, **kwargs):
        raise AssertionError("must not dispatch caller-defined methods")


def _run(brain: DeterministicBrain, **overrides: object):
    arguments = dict(
        run_id="safe-run",
        task_id="safe-task",
        state=WorldState(),
        goal=DeterministicGoal(),
        actions=(),
    )
    arguments.update(overrides)
    return asyncio.run(brain.run(**arguments))


@pytest.mark.parametrize(
    "value",
    [None, False, 1, 1.0, [], b"run", "", "   ", "\\ud800", HostileStr("run")],
)
def test_invalid_run_id_never_reaches_planner_or_journal(value: object) -> None:
    planner = CountingPlanner()
    journal = GuardedJournal()
    brain = DeterministicBrain(planner=planner, tools=ToolExecutor(), effect_journal=journal)
    with pytest.raises(ValueError, match="run_id"):
        _run(brain, run_id=value)
    assert planner.calls == 0
    assert journal.inspected is False


@pytest.mark.parametrize(
    "value",
    [None, False, 1, 1.0, [], b"task", "", "   ", "\\udfff", HostileStr("task")],
)
def test_invalid_task_id_never_reaches_planner_or_journal(value: object) -> None:
    planner = CountingPlanner()
    journal = GuardedJournal()
    brain = DeterministicBrain(planner=planner, tools=ToolExecutor(), effect_journal=journal)
    with pytest.raises(ValueError, match="task_id"):
        _run(brain, task_id=value)
    assert planner.calls == 0
    assert journal.inspected is False


def test_supplied_invalid_task_id_is_rejected_even_without_a_journal() -> None:
    planner = CountingPlanner()
    brain = DeterministicBrain(planner=planner, tools=ToolExecutor())
    with pytest.raises(ValueError, match="task_id"):
        _run(brain, task_id="\\ud800")
    assert planner.calls == 0


def test_valid_unicode_identities_preserve_planning_and_journal_inspection() -> None:
    planner = CountingPlanner()
    journal = GuardedJournal()
    brain = DeterministicBrain(planner=planner, tools=ToolExecutor(), effect_journal=journal)
    result = _run(brain, run_id="запуск 1", task_id="завдання 2")
    assert result.ok
    assert planner.calls == 1
    assert journal.inspected is True


def test_omitted_optional_task_id_preserves_no_journal_workflow() -> None:
    planner = CountingPlanner()
    brain = DeterministicBrain(planner=planner, tools=ToolExecutor())
    result = _run(brain, task_id=None)
    assert result.ok
    assert planner.calls == 1
