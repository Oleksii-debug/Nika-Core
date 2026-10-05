"""Fail closed on deterministic carrier aliasing and post-construction tamper."""

from __future__ import annotations

import asyncio

import pytest

from nika_core.intelligence.brain import DeterministicBrain
from nika_core.intelligence.contracts import (
    DeterministicAction,
    DeterministicEffectReservation,
    DeterministicEffectStatus,
    DeterministicErrorCode,
    DeterministicGoal,
    DeterministicPlan,
    PlanStep,
    WorldState,
)
from nika_core.tools import ToolExecutor, ToolRisk, ToolSpec


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


class ExplodingMapping(dict[str, object]):
    def __iter__(self):
        raise AssertionError("hostile action arguments were iterated")


def _run(
    brain: DeterministicBrain,
    *,
    state: WorldState | None = None,
    goal: DeterministicGoal | None = None,
    actions: tuple[DeterministicAction, ...] = (),
):
    return asyncio.run(
        brain.run(
            run_id="snapshot-proof",
            task_id="task-1",
            state=WorldState() if state is None else state,
            goal=DeterministicGoal() if goal is None else goal,
            actions=actions,
        )
    )


def test_post_construction_action_argument_swap_fails_before_journal_or_planner() -> None:
    planner = CountingPlanner()
    journal = GuardedJournal()
    action = DeterministicAction(action_id="safe")
    object.__setattr__(action, "arguments", ExplodingMapping({"payload": "changed"}))

    brain = DeterministicBrain(
        planner=planner,
        tools=ToolExecutor(),
        effect_journal=journal,
    )
    with pytest.raises(TypeError, match="arguments must be a frozen snapshot"):
        _run(brain, actions=(action,))

    assert planner.calls == 0
    assert journal.inspected is False


@pytest.mark.parametrize("carrier", ["state", "goal"])
def test_post_construction_context_tamper_fails_before_journal_or_planner(
    carrier: str,
) -> None:
    planner = CountingPlanner()
    journal = GuardedJournal()
    state = WorldState()
    goal = DeterministicGoal()
    if carrier == "state":
        object.__setattr__(state, "facts", {"tampered"})
    else:
        object.__setattr__(goal, "required", {"tampered"})

    brain = DeterministicBrain(
        planner=planner,
        tools=ToolExecutor(),
        effect_journal=journal,
    )
    with pytest.raises(TypeError):
        _run(brain, state=state, goal=goal)

    assert planner.calls == 0
    assert journal.inspected is False


def test_planner_receives_private_action_snapshot_not_caller_alias() -> None:
    original = DeterministicAction(
        action_id="advance",
        adds=frozenset({"done"}),
    )

    class MutatingPlanner:
        def plan(self, *, state, goal, actions) -> DeterministicPlan:
            assert actions[0] is not original
            object.__setattr__(original, "adds", frozenset({"hijacked"}))
            return DeterministicPlan(steps=(PlanStep(action_id="advance"),))

    result = asyncio.run(
        DeterministicBrain(
            planner=MutatingPlanner(),
            tools=ToolExecutor(),
        ).run(
            run_id="private-action-snapshot",
            state=WorldState(),
            goal=DeterministicGoal(required=frozenset({"done"})),
            actions=(original,),
        )
    )

    assert result.ok
    assert result.final_state.facts == frozenset({"done"})
    assert original.adds == frozenset({"hijacked"})


def test_tampered_planner_step_carrier_is_rejected_before_provenance_hashing() -> None:
    action = DeterministicAction(
        action_id="advance",
        adds=frozenset({"done"}),
    )
    step = PlanStep(action_id="advance")
    object.__setattr__(step, "action_id", 7)

    class TamperedPlanPlanner:
        def plan(self, *, state, goal, actions) -> DeterministicPlan:
            return DeterministicPlan(steps=(step,))

    brain = DeterministicBrain(
        planner=TamperedPlanPlanner(),
        tools=ToolExecutor(),
    )
    with pytest.raises(TypeError, match="plan action_id"):
        asyncio.run(
            brain.run(
                run_id="tampered-plan-step",
                state=WorldState(),
                goal=DeterministicGoal(required=frozenset({"done"})),
                actions=(action,),
            )
        )


def test_tampered_observed_state_is_normalized_to_fail_closed_result() -> None:
    action = DeterministicAction(
        action_id="advance",
        adds=frozenset({"done"}),
    )

    class OneStepPlanner:
        def plan(self, *, state, goal, actions) -> DeterministicPlan:
            return DeterministicPlan(steps=(PlanStep(action_id="advance"),))

    class TamperedObserver:
        async def observe(self) -> WorldState:
            observed = WorldState()
            object.__setattr__(observed, "facts", {"tampered"})
            return observed

    result = asyncio.run(
        DeterministicBrain(
            planner=OneStepPlanner(),
            tools=ToolExecutor(),
        ).run(
            run_id="tampered-observer",
            state=WorldState(),
            goal=DeterministicGoal(required=frozenset({"done"})),
            actions=(action,),
            state_observer=TamperedObserver(),
        )
    )

    assert not result.ok
    assert result.error_code is DeterministicErrorCode.STATE_OBSERVATION_FAILED
    assert result.completed_actions == ()


def test_planner_cannot_mutate_brain_execution_authority() -> None:
    original = DeterministicAction(
        action_id="advance",
        adds=frozenset({"done"}),
    )

    class AuthorityMutatingPlanner:
        def plan(self, *, state, goal, actions) -> DeterministicPlan:
            object.__setattr__(state, "facts", frozenset({"planner-state"}))
            object.__setattr__(goal, "required", frozenset({"planner-goal"}))
            object.__setattr__(actions[0], "adds", frozenset({"planner-goal"}))
            return DeterministicPlan(steps=(PlanStep(action_id="advance"),))

    result = asyncio.run(
        DeterministicBrain(
            planner=AuthorityMutatingPlanner(),
            tools=ToolExecutor(),
        ).run(
            run_id="planner-authority-isolation",
            state=WorldState(),
            goal=DeterministicGoal(required=frozenset({"done"})),
            actions=(original,),
        )
    )

    assert result.ok
    assert result.final_state.facts == frozenset({"done"})
    assert original.adds == frozenset({"done"})


def test_effect_journal_cannot_mutate_action_used_for_execution() -> None:
    class MutatingJournal:
        def unresolved_operation_keys(self, *, task_id: str) -> tuple[str, ...]:
            return ()

        def reserve(
            self,
            *,
            task_id: str,
            action: DeterministicAction,
        ) -> DeterministicEffectReservation:
            object.__setattr__(action, "adds", frozenset({"journal-hijacked"}))
            return DeterministicEffectReservation(
                operation_key="journal-isolation:1",
                status=DeterministicEffectStatus.PENDING,
                created=True,
            )

        def complete(self, operation_key: str) -> None:
            assert operation_key == "journal-isolation:1"

        def mark_uncertain(self, operation_key: str) -> None:
            raise AssertionError("effect should not become uncertain")

        def release_pending(self, operation_key: str) -> None:
            raise AssertionError("effect reservation should not be released")

    seen_arguments: list[dict[str, object]] = []

    async def write_result(arguments: dict[str, object]) -> object:
        seen_arguments.append(dict(arguments))
        return "written"

    tools = ToolExecutor()
    tools.register(
        ToolSpec(
            tool_id="write.result",
            description="write deterministic result",
            risk=ToolRisk.LOCAL_WRITE,
        ),
        write_result,
    )
    action = DeterministicAction(
        action_id="write",
        adds=frozenset({"done"}),
        tool_id="write.result",
        arguments={"target": "safe.txt"},
    )

    class WritePlanner:
        def plan(self, *, state, goal, actions) -> DeterministicPlan:
            return DeterministicPlan(
                steps=(PlanStep(action_id="write", tool_id="write.result"),)
            )

    result = asyncio.run(
        DeterministicBrain(
            planner=WritePlanner(),
            tools=tools,
            effect_journal=MutatingJournal(),
        ).run(
            run_id="journal-authority-isolation",
            task_id="journal-task",
            state=WorldState(),
            goal=DeterministicGoal(required=frozenset({"done"})),
            actions=(action,),
        )
    )

    assert result.ok
    assert result.final_state.facts == frozenset({"done"})
    assert seen_arguments == [{"target": "safe.txt"}]
