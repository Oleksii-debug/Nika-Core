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

    def plan(
        self,
        *,
        state: WorldState,
        goal: DeterministicGoal,
        actions: tuple[DeterministicAction, ...],
    ) -> DeterministicPlan:
        self.calls += 1
        return DeterministicPlan(steps=(PlanStep(action_id="finish"),))


_ACTIONS = (
    DeterministicAction(action_id="prepare", adds=frozenset({"prepared"})),
    DeterministicAction(
        action_id="finish",
        requires=frozenset({"prepared"}),
        adds=frozenset({"finished"}),
    ),
)


def _resume(*, max_steps: int, goal: str) -> tuple[object, CountingPlanner]:
    planner = CountingPlanner()
    brain = DeterministicBrain(planner=planner, tools=ToolExecutor())
    result = asyncio.run(
        brain.run(
            run_id="restart-budget",
            state=WorldState(facts=frozenset({"prepared"})),
            goal=DeterministicGoal(required=frozenset({goal})),
            actions=_ACTIONS,
            previously_completed_action_ids=("prepare",),
            max_steps=max_steps,
        )
    )
    return result, planner


def test_recovered_action_exhausts_total_budget_before_planner() -> None:
    result, planner = _resume(max_steps=1, goal="finished")
    assert not result.ok
    assert result.error_code is DeterministicErrorCode.PLAN_TOO_LONG
    assert result.completed_actions == ("prepare",)
    assert planner.calls == 0


def test_exact_budget_completed_checkpoint_is_terminal_without_replanning() -> None:
    result, planner = _resume(max_steps=1, goal="prepared")
    assert result.ok
    assert result.completed_actions == ("prepare",)
    assert result.final_state.facts == frozenset({"prepared"})
    assert planner.calls == 0
    assert result.planning_history == ()


def test_satisfied_recovered_goal_below_step_limit_skips_planner() -> None:
    # Recovery must not invoke a planner (and thereby risk a new effect) merely
    # because some of the original task-wide step allowance remains unused.
    result, planner = _resume(max_steps=2, goal="prepared")
    assert result.ok
    assert result.completed_actions == ("prepare",)
    assert result.final_state.facts == frozenset({"prepared"})
    assert result.planning_history == ()
    assert planner.calls == 0


def test_satisfied_recovered_goal_with_spare_budget_requires_observer_confirmation() -> None:
    class ConfirmingObserver:
        def __init__(self) -> None:
            self.calls = 0

        async def observe(self) -> WorldState:
            self.calls += 1
            return WorldState(facts=frozenset({"prepared", "finished"}))

    observer = ConfirmingObserver()
    planner = CountingPlanner()
    result = asyncio.run(
        DeterministicBrain(planner=planner, tools=ToolExecutor()).run(
            run_id="spare-budget-confirmation",
            state=WorldState(facts=frozenset({"prepared", "finished"})),
            goal=DeterministicGoal(required=frozenset({"finished"})),
            actions=_ACTIONS,
            previously_completed_action_ids=("prepare",),
            max_steps=2,
            state_observer=observer,
        )
    )
    assert result.ok
    assert result.completed_actions == ("prepare",)
    assert observer.calls == 1
    assert planner.calls == 0


def test_satisfied_recovered_goal_with_spare_budget_replans_from_observed_drift() -> None:
    class DriftingObserver:
        def __init__(self) -> None:
            self.calls = 0

        async def observe(self) -> WorldState:
            self.calls += 1
            if self.calls < 3:
                return WorldState(facts=frozenset({"prepared"}))
            return WorldState(facts=frozenset({"prepared", "finished"}))

    observer = DriftingObserver()
    planner = CountingPlanner()
    result = asyncio.run(
        DeterministicBrain(planner=planner, tools=ToolExecutor()).run(
            run_id="spare-budget-drift",
            state=WorldState(facts=frozenset({"prepared", "finished"})),
            goal=DeterministicGoal(required=frozenset({"finished"})),
            actions=_ACTIONS,
            previously_completed_action_ids=("prepare",),
            max_steps=2,
            state_observer=observer,
        )
    )
    assert result.ok
    assert result.completed_actions == ("prepare", "finish")
    assert observer.calls == 3
    assert planner.calls == 1


def test_recovered_action_leaves_only_one_new_execution_slot() -> None:
    result, planner = _resume(max_steps=2, goal="finished")
    assert result.ok
    assert result.completed_actions == ("prepare", "finish")
    assert planner.calls == 1


def test_corrupt_checkpoint_exceeding_budget_cannot_claim_success() -> None:
    planner = CountingPlanner()
    brain = DeterministicBrain(planner=planner, tools=ToolExecutor())
    result = asyncio.run(
        brain.run(
            run_id="excess-checkpoint",
            state=WorldState(facts=frozenset({"prepared", "finished"})),
            goal=DeterministicGoal(required=frozenset({"finished"})),
            actions=_ACTIONS,
            previously_completed_action_ids=("prepare", "finish"),
            max_steps=1,
        )
    )
    assert not result.ok
    assert result.error_code is DeterministicErrorCode.PLAN_TOO_LONG
    assert planner.calls == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_steps": True},
        {"max_steps": 1.5},
        {"max_replans": False},
        {"max_replans": 0.5},
        {"planning_timeout_seconds": float("nan")},
        {"planning_timeout_seconds": float("inf")},
        {"planning_timeout_seconds": 10**1000},
        {"observation_timeout_seconds": float("-inf")},
        {"observation_timeout_seconds": True},
    ],
)
def test_noncanonical_budgets_fail_before_planning(overrides: dict[str, object]) -> None:
    planner = CountingPlanner()
    brain = DeterministicBrain(planner=planner, tools=ToolExecutor())
    with pytest.raises(ValueError):
        asyncio.run(
            brain.run(
                run_id="unsafe-budget",
                state=WorldState(),
                goal=DeterministicGoal(required=frozenset({"finished"})),
                actions=_ACTIONS,
                **overrides,
            )
        )
    assert planner.calls == 0


def test_unresolved_effect_overrides_exact_budget_terminal_success() -> None:
    class UnresolvedJournal:
        def unresolved_operation_keys(self, *, task_id: str) -> tuple[str, ...]:
            assert task_id == "task-1"
            return ("pending-operation",)

    planner = CountingPlanner()
    brain = DeterministicBrain(
        planner=planner,
        tools=ToolExecutor(),
        effect_journal=UnresolvedJournal(),  # type: ignore[arg-type]
    )
    result = asyncio.run(
        brain.run(
            run_id="unsafe-restart",
            task_id="task-1",
            state=WorldState(facts=frozenset({"prepared"})),
            goal=DeterministicGoal(required=frozenset({"prepared"})),
            actions=_ACTIONS,
            previously_completed_action_ids=("prepare",),
            max_steps=1,
        )
    )
    assert not result.ok
    assert result.error_code is DeterministicErrorCode.SIDE_EFFECT_RECONCILIATION_REQUIRED
    assert planner.calls == 0


@pytest.mark.parametrize(
    "identifier",
    [" spaced", "bidi\u202e", "e\u0301", "x" * 513, "\ud800"],
)

def test_adversarial_run_identity_cannot_start_planner(identifier: str) -> None:
    planner = CountingPlanner()
    brain = DeterministicBrain(planner=planner, tools=ToolExecutor())
    with pytest.raises(ValueError, match="canonical bounded UTF-8"):
        asyncio.run(
            brain.run(
                run_id=identifier,
                state=WorldState(),
                goal=DeterministicGoal(required=frozenset({"finished"})),
                actions=_ACTIONS,
            )
        )
    assert planner.calls == 0


def test_no_journal_task_id_is_still_canonical() -> None:
    planner = CountingPlanner()
    brain = DeterministicBrain(planner=planner, tools=ToolExecutor())
    with pytest.raises(ValueError, match="task_id"):
        asyncio.run(
            brain.run(
                run_id="valid-run",
                task_id="bad\u202e",
                state=WorldState(),
                goal=DeterministicGoal(required=frozenset({"finished"})),
                actions=_ACTIONS,
            )
        )
    assert planner.calls == 0


def test_action_and_checkpoint_ids_are_rejected_before_planner() -> None:
    planner = CountingPlanner()
    brain = DeterministicBrain(planner=planner, tools=ToolExecutor())
    rogue_action = DeterministicAction(action_id="rogue\u202e")
    with pytest.raises(ValueError, match="action_id"):
        asyncio.run(
            brain.run(
                run_id="valid-run",
                state=WorldState(),
                goal=DeterministicGoal(required=frozenset({"finished"})),
                actions=(rogue_action,),
            )
        )
    with pytest.raises(ValueError, match="previously_completed_action_id"):
        asyncio.run(
            brain.run(
                run_id="valid-run",
                state=WorldState(),
                goal=DeterministicGoal(required=frozenset({"finished"})),
                actions=_ACTIONS,
                previously_completed_action_ids=("rogue\u202e",),
            )
        )
    assert planner.calls == 0


def test_terminal_recovery_reobserves_authoritative_state_before_success() -> None:
    class DriftObserver:
        def __init__(self) -> None:
            self.calls = 0

        async def observe(self) -> WorldState:
            self.calls += 1
            return WorldState()  # The previously satisfied fact has disappeared.

    observer = DriftObserver()
    planner = CountingPlanner()
    result = asyncio.run(
        DeterministicBrain(planner=planner, tools=ToolExecutor()).run(
            run_id="observed-restart-drift",
            state=WorldState(facts=frozenset({"prepared"})),
            goal=DeterministicGoal(required=frozenset({"prepared"})),
            actions=_ACTIONS,
            previously_completed_action_ids=("prepare",),
            max_steps=1,
            state_observer=observer,
        )
    )
    assert not result.ok
    assert result.error_code is DeterministicErrorCode.PLAN_TOO_LONG
    assert result.completed_actions == ("prepare",)
    assert result.final_state == WorldState()
    assert observer.calls == 1
    assert planner.calls == 0


def test_terminal_recovery_observer_failure_blocks_checkpoint_success() -> None:
    class FailingObserver:
        async def observe(self) -> WorldState:
            raise RuntimeError("state unavailable")

    planner = CountingPlanner()
    result = asyncio.run(
        DeterministicBrain(planner=planner, tools=ToolExecutor()).run(
            run_id="observed-restart-error",
            state=WorldState(facts=frozenset({"prepared"})),
            goal=DeterministicGoal(required=frozenset({"prepared"})),
            actions=_ACTIONS,
            previously_completed_action_ids=("prepare",),
            max_steps=1,
            state_observer=FailingObserver(),
        )
    )
    assert not result.ok
    assert result.error_code is DeterministicErrorCode.STATE_OBSERVATION_FAILED
    assert result.completed_actions == ("prepare",)
    assert planner.calls == 0


def test_terminal_recovery_observer_can_confirm_checkpoint_without_planner() -> None:
    class ConfirmingObserver:
        def __init__(self) -> None:
            self.calls = 0

        async def observe(self) -> WorldState:
            self.calls += 1
            return WorldState(facts=frozenset({"prepared"}))

    observer = ConfirmingObserver()
    planner = CountingPlanner()
    result = asyncio.run(
        DeterministicBrain(planner=planner, tools=ToolExecutor()).run(
            run_id="observed-restart-confirmed",
            state=WorldState(facts=frozenset({"prepared"})),
            goal=DeterministicGoal(required=frozenset({"prepared"})),
            actions=_ACTIONS,
            previously_completed_action_ids=("prepare",),
            max_steps=1,
            state_observer=observer,
        )
    )
    assert result.ok
    assert result.completed_actions == ("prepare",)
    assert observer.calls == 1
    assert planner.calls == 0

def test_consumable_recovery_iterator_cannot_reset_completed_step_budget() -> None:
    planner = CountingPlanner()
    brain = DeterministicBrain(planner=planner, tools=ToolExecutor())
    checkpoint = (item for item in ("prepare",))
    with pytest.raises(ValueError, match="previously_completed_action_ids"):
        asyncio.run(
            brain.run(
                run_id="generator-checkpoint",
                state=WorldState(facts=frozenset({"prepared"})),
                goal=DeterministicGoal(required=frozenset({"finished"})),
                actions=_ACTIONS,
                previously_completed_action_ids=checkpoint,  # type: ignore[arg-type]
                max_steps=1,
            )
        )
    assert planner.calls == 0


def test_mutable_recovery_sequence_is_not_trusted_as_checkpoint_authority() -> None:
    planner = CountingPlanner()
    brain = DeterministicBrain(planner=planner, tools=ToolExecutor())
    with pytest.raises(ValueError, match="previously_completed_action_ids"):
        asyncio.run(
            brain.run(
                run_id="mutable-checkpoint",
                state=WorldState(facts=frozenset({"prepared"})),
                goal=DeterministicGoal(required=frozenset({"finished"})),
                actions=_ACTIONS,
                previously_completed_action_ids=["prepare"],  # type: ignore[arg-type]
                max_steps=1,
            )
        )
    assert planner.calls == 0


def test_consumable_action_catalog_is_rejected_before_planning() -> None:
    planner = CountingPlanner()
    brain = DeterministicBrain(planner=planner, tools=ToolExecutor())
    with pytest.raises(ValueError, match="actions must be an immutable tuple"):
        asyncio.run(
            brain.run(
                run_id="generator-actions",
                state=WorldState(),
                goal=DeterministicGoal(required=frozenset({"finished"})),
                actions=(action for action in _ACTIONS),  # type: ignore[arg-type]
            )
        )
    assert planner.calls == 0
