from __future__ import annotations

from types import SimpleNamespace
from typing import Self

import pytest

from nika_core.intelligence.contracts import (
    DeterministicAction,
    DeterministicGoal,
    DeterministicPlan,
    PlanStep,
    WorldState,
    canonicalize_planner_inputs,
)
from nika_core.intelligence.unified_planning_adapter import UnifiedPlanningAdapter


class _FakeFluent:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeAction:
    def __init__(self, name: str) -> None:
        self.name = name
        self.preconditions: list[object] = []
        self.effects: list[tuple[str, bool]] = []

    def add_precondition(self, condition: object) -> None:
        self.preconditions.append(condition)

    def add_effect(self, fluent: _FakeFluent, value: bool) -> None:
        self.effects.append((fluent.name, value))


class _FakeProblem:
    def __init__(self, name: str) -> None:
        self.name = name
        self.fluents: list[str] = []
        self.initial_values: list[tuple[str, bool]] = []
        self.actions: list[_FakeAction] = []
        self.goals: list[object] = []

    def add_fluent(self, fluent: _FakeFluent, *, default_initial_value: bool) -> None:
        assert default_initial_value is False
        self.fluents.append(fluent.name)

    def set_initial_value(self, fluent: _FakeFluent, value: bool) -> None:
        self.initial_values.append((fluent.name, value))

    def add_action(self, action: _FakeAction) -> None:
        self.actions.append(action)

    def add_goal(self, goal: object) -> None:
        self.goals.append(goal)

    def signature(self) -> tuple[object, ...]:
        return (
            tuple(self.fluents),
            tuple(self.initial_values),
            tuple(
                (action.name, tuple(action.preconditions), tuple(action.effects))
                for action in self.actions
            ),
            tuple(self.goals),
        )


class _FakePlanner:
    def __init__(self, captures: list[tuple[object, ...]]) -> None:
        self._captures = captures

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback

    def solve(self, problem: _FakeProblem) -> SimpleNamespace:
        self._captures.append(problem.signature())
        return SimpleNamespace(
            status=SimpleNamespace(name="SOLVED_SATISFICING"),
            plan=SimpleNamespace(
                actions=(SimpleNamespace(action=problem.actions[0]),),
            ),
        )


class _FakeShortcuts:
    def __init__(self, captures: list[tuple[object, ...]]) -> None:
        self._captures = captures

    @staticmethod
    def BoolType() -> str:
        return "bool"

    @staticmethod
    def Fluent(name: str, _kind: object) -> _FakeFluent:
        return _FakeFluent(name)

    @staticmethod
    def InstantaneousAction(name: str) -> _FakeAction:
        return _FakeAction(name)

    @staticmethod
    def Not(value: _FakeFluent) -> tuple[str, str]:
        return ("not", value.name)

    @staticmethod
    def Or(*values: object) -> tuple[str, tuple[object, ...]]:
        return ("or", values)

    def Problem(self, name: str) -> _FakeProblem:
        return _FakeProblem(name)

    def OneshotPlanner(self, *, name: str) -> _FakePlanner:
        assert name == "aries"
        return _FakePlanner(self._captures)


def _equivalent_inputs(*, reversed_order: bool) -> tuple[
    WorldState,
    DeterministicGoal,
    tuple[DeterministicAction, ...],
]:
    state_facts = [" beta ", "alpha"] if reversed_order else ["alpha", "beta"]
    goal_facts = [" ready ", "done"] if reversed_order else ["done", "ready"]
    requires = ["beta", " alpha "] if reversed_order else ["alpha", "beta"]
    forbids = ["blocked-2", " blocked-1 "] if reversed_order else ["blocked-1", "blocked-2"]
    adds = ["ready", " done "] if reversed_order else ["done", "ready"]

    alpha = DeterministicAction(
        action_id=" alpha-finish " if reversed_order else "alpha-finish",
        requires=requires,  # type: ignore[arg-type]
        forbids=forbids,  # type: ignore[arg-type]
        adds=adds,  # type: ignore[arg-type]
    )
    zeta = DeterministicAction(
        action_id="zeta-finish",
        requires=list(reversed(requires)),  # type: ignore[arg-type]
        forbids=list(reversed(forbids)),  # type: ignore[arg-type]
        adds=list(reversed(adds)),  # type: ignore[arg-type]
    )
    actions = (zeta, alpha) if reversed_order else (alpha, zeta)
    return (
        WorldState(state_facts),  # type: ignore[arg-type]
        DeterministicGoal(required=goal_facts),  # type: ignore[arg-type]
        actions,
    )


def test_semantically_equal_serializations_canonicalize_identically() -> None:
    first_state, first_goal, first_actions = _equivalent_inputs(reversed_order=False)
    second_state, second_goal, second_actions = _equivalent_inputs(reversed_order=True)
    first = canonicalize_planner_inputs(
        state=first_state,
        goal=first_goal,
        actions=first_actions,
    )
    second = canonicalize_planner_inputs(
        state=second_state,
        goal=second_goal,
        actions=second_actions,
    )

    assert first == second
    assert tuple(action.action_id for action in first[2]) == ("alpha-finish", "zeta-finish")


def test_adapter_builds_same_problem_and_plan_for_equivalent_input_orders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captures: list[tuple[object, ...]] = []
    fake_shortcuts = _FakeShortcuts(captures)
    monkeypatch.setattr(
        UnifiedPlanningAdapter,
        "_shortcuts",
        staticmethod(lambda: fake_shortcuts),
    )
    planner = UnifiedPlanningAdapter()

    first_state, first_goal, first_actions = _equivalent_inputs(reversed_order=False)
    second_state, second_goal, second_actions = _equivalent_inputs(reversed_order=True)
    first_plan = planner.plan(state=first_state, goal=first_goal, actions=first_actions)
    second_plan = planner.plan(state=second_state, goal=second_goal, actions=second_actions)

    expected = DeterministicPlan(steps=(PlanStep(action_id="alpha-finish"),))
    assert first_plan == expected
    assert second_plan == expected
    assert captures[0] == captures[1]


def test_empty_goal_is_rejected_before_solver_import(monkeypatch: pytest.MonkeyPatch) -> None:
    solver_touched = False

    def fail_if_solver_is_touched() -> object:
        nonlocal solver_touched
        solver_touched = True
        raise AssertionError("solver must not be touched for an invalid empty goal")

    monkeypatch.setattr(
        UnifiedPlanningAdapter,
        "_shortcuts",
        staticmethod(fail_if_solver_is_touched),
    )

    with pytest.raises(ValueError, match="planner goal must contain at least one constraint"):
        UnifiedPlanningAdapter().plan(
            state=WorldState(),
            goal=DeterministicGoal(),
            actions=(),
        )

    assert solver_touched is False


def test_duplicate_constraints_are_rejected_after_normalization() -> None:
    with pytest.raises(ValueError, match="duplicate goal required constraint: ready"):
        DeterministicGoal(required=["ready", " ready "])  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="duplicate action requires constraint: ready"):
        DeterministicAction(
            action_id="finish",
            requires=["ready", " ready "],  # type: ignore[arg-type]
            adds=frozenset({"done"}),
        )


def test_duplicate_capability_ids_are_rejected_after_normalization() -> None:
    first = DeterministicAction(action_id=" finish ", adds=frozenset({"done"}))
    second = DeterministicAction(action_id="finish", adds=frozenset({"done"}))

    with pytest.raises(ValueError, match="duplicate deterministic action_id"):
        canonicalize_planner_inputs(
            state=WorldState(),
            goal=DeterministicGoal(required=frozenset({"done"})),
            actions=(first, second),
        )


def test_malformed_capability_records_are_rejected() -> None:
    with pytest.raises(TypeError, match="planner capability records must be DeterministicAction"):
        canonicalize_planner_inputs(
            state=WorldState(),
            goal=DeterministicGoal(required=frozenset({"done"})),
            actions=({"action_id": "finish"},),  # type: ignore[arg-type]
        )

    with pytest.raises(TypeError, match="action arguments must be a dictionary"):
        DeterministicAction(
            action_id="finish",
            adds=frozenset({"done"}),
            arguments=[],  # type: ignore[arg-type]
        )
