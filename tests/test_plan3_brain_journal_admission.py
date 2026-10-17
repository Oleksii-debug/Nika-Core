from __future__ import annotations

import asyncio

import pytest

from nika_core.intelligence.brain import DeterministicBrain
from nika_core.intelligence.contracts import (
    DeterministicAction,
    DeterministicErrorCode,
    DeterministicGoal,
    WorldState,
)
from nika_core.tools import ToolExecutor


@pytest.mark.parametrize(
    "malformed",
    [
        ["deterministic:pending"],
        "deterministic:pending",
        ("",),
        ("deterministic:\nspoof",),
        ("deterministic:\u202espoof",),
        (123,),
        tuple("deterministic:pending" for _ in range(10_001)),
    ],
)
def test_corrupt_unresolved_journal_evidence_fails_before_planning_or_effects(
    malformed: object,
) -> None:
    calls: list[str] = []

    class ForbiddenPlanner:
        def plan(self, *, state: object, goal: object, actions: object) -> object:
            calls.append("planner")
            raise AssertionError("corrupt recovery evidence reached planner")

    class CorruptJournal:
        def unresolved_operation_keys(self, *, task_id: str) -> object:
            assert task_id == "restart-task"
            calls.append("inspection")
            return malformed

        def reserve(self, **kwargs: object) -> object:
            calls.append("reservation")
            raise AssertionError("corrupt recovery evidence reached reservation")

    result = asyncio.run(
        DeterministicBrain(
            planner=ForbiddenPlanner(),
            tools=ToolExecutor(),
            effect_journal=CorruptJournal(),  # type: ignore[arg-type]
        ).run(
            run_id="restart-run",
            task_id="restart-task",
            state=WorldState(),
            goal=DeterministicGoal(required=frozenset({"done"})),
            actions=(DeterministicAction(action_id="finish", adds=frozenset({"done"})),),
        )
    )
    assert result.error_code is DeterministicErrorCode.SIDE_EFFECT_RECORD_FAILED
    assert result.completed_actions == ()
    assert result.planning_history == ()
    assert result.final_state == WorldState()
    assert calls == ["inspection"]


def test_valid_pending_journal_evidence_still_requires_reconciliation() -> None:
    class PendingJournal:
        def unresolved_operation_keys(self, *, task_id: str) -> tuple[str, ...]:
            assert task_id == "restart-task"
            return ("deterministic:" + "f" * 64,)

    class ForbiddenPlanner:
        def plan(self, **kwargs: object) -> object:
            raise AssertionError("pending effect must block planner")

    result = asyncio.run(
        DeterministicBrain(
            planner=ForbiddenPlanner(),
            tools=ToolExecutor(),
            effect_journal=PendingJournal(),  # type: ignore[arg-type]
        ).run(
            run_id="restart-run",
            task_id="restart-task",
            state=WorldState(),
            goal=DeterministicGoal(required=frozenset({"done"})),
            actions=(DeterministicAction(action_id="finish", adds=frozenset({"done"})),),
        )
    )
    assert result.error_code is DeterministicErrorCode.SIDE_EFFECT_RECONCILIATION_REQUIRED
    assert result.completed_actions == ()
