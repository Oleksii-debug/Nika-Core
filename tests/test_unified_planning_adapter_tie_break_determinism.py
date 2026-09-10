from __future__ import annotations

from random import Random
from types import SimpleNamespace

import pytest

from nika_core.intelligence.contracts import DeterministicAction, DeterministicGoal, WorldState
from nika_core.intelligence.unified_planning_adapter import UnifiedPlanningAdapter


class _FakeFluent:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeExpression:
    def __init__(self, kind: str, *children: object) -> None:
        self.kind = kind
        self.children = children


class _FakeAction:
    def __init__(self, name: str) -> None:
        self.name = name
        self.preconditions: list[object] = []
        self.effects: list[tuple[_FakeFluent, bool]] = []

    def add_precondition(self, expression: object) -> None:
        self.preconditions.append(expression)

    def add_effect(self, fluent: _FakeFluent, value: bool) -> None:
        self.effects.append((fluent, value))


class _FakeProblem:
    def __init__(self, name: str) -> None:
        self.name = name
        self.initial_values: list[tuple[_FakeFluent, bool]] = []
        self.actions: list[_FakeAction] = []
        self.goals: list[object] = []

    def add_fluent(self, _fluent: _FakeFluent, *, default_initial_value: bool) -> None:
        assert default_initial_value is False

    def set_initial_value(self, fluent: _FakeFluent, value: bool) -> None:
        self.initial_values.append((fluent, value))

    def add_action(self, action: _FakeAction) -> None:
        self.actions.append(action)

    def add_goal(self, goal: object) -> None:
        self.goals.append(goal)


class _FakePlanner:
    def __init__(self, shortcuts: _FakeShortcuts) -> None:
        self._shortcuts = shortcuts

    def __enter__(self) -> _FakePlanner:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def solve(self, problem: _FakeProblem) -> object:
        self._shortcuts.problem_signatures.append(_problem_signature(problem))
        selected = problem.actions[0]
        return SimpleNamespace(
            status=SimpleNamespace(name="SOLVED_SATISFICING"),
            plan=SimpleNamespace(actions=(SimpleNamespace(action=selected),)),
        )


class _FakeShortcuts:
    def __init__(self) -> None:
        self.problem_signatures: list[tuple[object, ...]] = []

    @staticmethod
    def BoolType() -> object:
        return object()

    @staticmethod
    def Fluent(name: str, _bool_type: object) -> _FakeFluent:
        return _FakeFluent(name)

    @staticmethod
    def Not(expression: object) -> _FakeExpression:
        return _FakeExpression("not", expression)

    @staticmethod
    def Or(*expressions: object) -> _FakeExpression:
        return _FakeExpression("or", *expressions)

    @staticmethod
    def Problem(name: str) -> _FakeProblem:
        return _FakeProblem(name)

    @staticmethod
    def InstantaneousAction(name: str) -> _FakeAction:
        return _FakeAction(name)

    def OneshotPlanner(self, *, name: str) -> _FakePlanner:
        assert name == "aries"
        return _FakePlanner(self)


def _expression_signature(expression: object) -> object:
    if isinstance(expression, _FakeFluent):
        return ("fluent", expression.name)
    if isinstance(expression, _FakeExpression):
        return (
            expression.kind,
            tuple(_expression_signature(child) for child in expression.children),
        )
    raise AssertionError(f"unexpected fake expression: {expression!r}")


def _problem_signature(problem: _FakeProblem) -> tuple[object, ...]:
    return (
        tuple(
            (fluent.name, value)
            for fluent, value in problem.initial_values
        ),
        tuple(
            (
                action.name,
                tuple(_expression_signature(item) for item in action.preconditions),
                tuple(
                    (_expression_signature(fluent), value)
                    for fluent, value in action.effects
                ),
            )
            for action in problem.actions
        ),
        tuple(_expression_signature(goal) for goal in problem.goals),
    )


def _shuffled_frozenset(values: tuple[str, ...], rng: Random) -> frozenset[str]:
    shuffled = list(values)
    rng.shuffle(shuffled)
    return frozenset(shuffled)


def _equivalent_action(action_id: str, rng: Random) -> DeterministicAction:
    return DeterministicAction(
        action_id=action_id,
        requires=_shuffled_frozenset(("ready-a", "ready-b", "ready-c"), rng),
        forbids=_shuffled_frozenset(("blocked-a", "blocked-b", "blocked-c"), rng),
        adds=_shuffled_frozenset(("goal-a", "goal-b", "goal-c"), rng),
        removes=_shuffled_frozenset(("stale-a", "stale-b", "stale-c"), rng),
    )


def test_equivalent_inputs_have_stable_solver_problem_and_tie_break(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_up = _FakeShortcuts()
    monkeypatch.setattr(
        UnifiedPlanningAdapter,
        "_shortcuts",
        staticmethod(lambda: fake_up),
    )
    planner = UnifiedPlanningAdapter()
    plan_evidence: set[tuple[str, ...]] = set()

    for seed in range(64):
        rng = Random(seed)
        actions = [
            _equivalent_action("route-b", rng),
            _equivalent_action("route-a", rng),
        ]
        rng.shuffle(actions)
        state = WorldState(
            _shuffled_frozenset(
                ("ready-a", "ready-b", "ready-c", "stale-a", "stale-b", "stale-c"),
                rng,
            )
        )
        goal = DeterministicGoal(
            required=_shuffled_frozenset(("goal-a", "goal-b", "goal-c"), rng),
            forbidden=_shuffled_frozenset(("stale-a", "stale-b", "stale-c"), rng),
        )

        plan = planner.plan(state=state, goal=goal, actions=tuple(actions))
        plan_evidence.add(tuple(step.action_id for step in plan.steps))

    assert plan_evidence == {("route-a",)}
    assert len(fake_up.problem_signatures) == 64
    assert len(set(fake_up.problem_signatures)) == 1
