from __future__ import annotations

from types import SimpleNamespace

import pytest

from nika_core.intelligence.contracts import (
    DeterministicAction,
    DeterministicErrorCode,
    DeterministicGoal,
    DeterministicPlanningError,
    WorldState,
)
from nika_core.intelligence.unified_planning_adapter import UnifiedPlanningAdapter


def _planner_must_not_start() -> object:
    raise AssertionError("planner engine must not start for a bounded precheck result")


def test_cyclic_impossible_goal_exhausts_without_starting_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    planner = UnifiedPlanningAdapter(max_expansions=2)
    monkeypatch.setattr(planner, "_shortcuts", _planner_must_not_start)
    actions = (
        DeterministicAction(
            action_id="turn-off",
            requires=frozenset({"x"}),
            removes=frozenset({"x"}),
        ),
        DeterministicAction(action_id="turn-on", adds=frozenset({"x"})),
        # Keep the target syntactically addable so the existing obvious-unreachable check
        # cannot decide this fixture before the bounded state-space check does.
        DeterministicAction(
            action_id="make-y",
            requires=frozenset({"done"}),
            adds=frozenset({"y"}),
        ),
        DeterministicAction(
            action_id="finish",
            requires=frozenset({"y"}),
            adds=frozenset({"done"}),
        ),
    )

    with pytest.raises(DeterministicPlanningError) as raised:
        planner.plan(
            state=WorldState(frozenset({"x"})),
            goal=DeterministicGoal(required=frozenset({"done"})),
            actions=actions,
        )

    assert raised.value.code is DeterministicErrorCode.GOAL_UNREACHABLE
    assert "finite deterministic action state space" in str(raised.value)


def test_branching_impossible_goal_hits_expansion_budget_before_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    planner = UnifiedPlanningAdapter(max_expansions=2)
    monkeypatch.setattr(planner, "_shortcuts", _planner_must_not_start)
    actions = (
        DeterministicAction(action_id="add-x", adds=frozenset({"x"})),
        DeterministicAction(action_id="add-y", adds=frozenset({"y"})),
        DeterministicAction(
            action_id="make-z",
            requires=frozenset({"done"}),
            adds=frozenset({"z"}),
        ),
        DeterministicAction(
            action_id="finish",
            requires=frozenset({"z"}),
            adds=frozenset({"done"}),
        ),
    )

    with pytest.raises(DeterministicPlanningError) as raised:
        planner.plan(
            state=WorldState(),
            goal=DeterministicGoal(required=frozenset({"done"})),
            actions=actions,
        )

    assert raised.value.code is DeterministicErrorCode.PLANNER_RESOURCE_LIMIT
    assert str(raised.value).endswith("max_expansions budget: 2")


def test_large_linear_fixture_stays_within_expansion_bound() -> None:
    actions = tuple(
        DeterministicAction(
            action_id=f"step-{index:04d}",
            requires=(
                frozenset() if index == 0 else frozenset({f"fact-{index - 1:04d}"})
            ),
            adds=frozenset({f"fact-{index:04d}"}),
        )
        for index in range(1_000)
    )

    UnifiedPlanningAdapter(max_expansions=1_000)._bounded_reachability_precheck(
        state=WorldState(),
        goal=DeterministicGoal(required=frozenset({"fact-0999"})),
        actions=actions,
    )


class _FakeFluent:
    def __init__(self, name: str, *_args: object) -> None:
        self.name = name


class _FakeAction:
    def __init__(self, name: str) -> None:
        self.name = name
        self.preconditions: list[object] = []
        self.effects: list[tuple[object, object]] = []

    def add_precondition(self, precondition: object) -> None:
        self.preconditions.append(precondition)

    def add_effect(self, fluent: object, value: object) -> None:
        self.effects.append((fluent, value))


class _FakeProblem:
    def __init__(self, name: str) -> None:
        self.name = name
        self.fluents: list[tuple[_FakeFluent, bool]] = []
        self.actions: list[_FakeAction] = []
        self.goals: list[object] = []

    def add_fluent(self, fluent: _FakeFluent, *, default_initial_value: bool = False) -> None:
        self.fluents.append((fluent, default_initial_value))

    def set_initial_value(self, *_args: object) -> None:
        return

    def add_action(self, action: _FakeAction) -> None:
        self.actions.append(action)

    def add_goal(self, goal: object) -> None:
        self.goals.append(goal)


class _FakePlanner:
    def __init__(self, record: dict[str, object]) -> None:
        self._record = record

    def __enter__(self) -> _FakePlanner:
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    def solve(self, problem: _FakeProblem, **kwargs: object) -> object:
        self._record["problem"] = problem
        self._record["kwargs"] = kwargs
        return SimpleNamespace(
            status=SimpleNamespace(name="SOLVED_SATISFICING"),
            plan=SimpleNamespace(
                actions=[SimpleNamespace(action=SimpleNamespace(name="action_0"))]
            ),
        )


def test_solver_problem_forbids_action_reuse_and_passes_native_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record: dict[str, object] = {}
    fake_up = SimpleNamespace(
        Problem=_FakeProblem,
        Fluent=_FakeFluent,
        BoolType=lambda: object(),
        InstantaneousAction=_FakeAction,
        Not=lambda item: ("not", item),
        Or=lambda *items: ("or", items),
        OneshotPlanner=lambda **_kwargs: _FakePlanner(record),
    )
    planner = UnifiedPlanningAdapter(solver_timeout_seconds=7.5)
    monkeypatch.setattr(planner, "_shortcuts", lambda: fake_up)

    plan = planner.plan(
        state=WorldState(),
        goal=DeterministicGoal(required=frozenset({"done"})),
        actions=(DeterministicAction(action_id="finish", adds=frozenset({"done"})),),
    )

    assert tuple(step.action_id for step in plan.steps) == ("finish",)
    assert record["kwargs"] == {"timeout": 7.5}
    problem = record["problem"]
    assert isinstance(problem, _FakeProblem)
    action = problem.actions[0]
    used_fluents = [
        fluent for fluent, _default in problem.fluents if fluent.name == "used_action_0"
    ]
    assert len(used_fluents) == 1
    assert ("not", used_fluents[0]) in action.preconditions
    assert (used_fluents[0], True) in action.effects
