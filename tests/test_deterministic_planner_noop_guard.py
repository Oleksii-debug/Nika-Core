from __future__ import annotations

from nika_core.intelligence.contracts import (
    DeterministicAction,
    DeterministicGoal,
    WorldState,
)
from nika_core.intelligence.unified_planning_adapter import UnifiedPlanningAdapter


def test_noop_guard_is_dynamic_and_keeps_later_state_changing_action() -> None:
    planner = UnifiedPlanningAdapter()
    actions = (
        DeterministicAction(
            action_id="disable-and-mark",
            requires=frozenset({"enabled"}),
            removes=frozenset({"enabled"}),
            adds=frozenset({"marked"}),
        ),
        DeterministicAction(
            action_id="reenable",
            adds=frozenset({"enabled"}),
        ),
    )

    plan = planner.plan(
        state=WorldState(frozenset({"enabled"})),
        goal=DeterministicGoal(required=frozenset({"enabled", "marked"})),
        actions=actions,
    )

    assert tuple(step.action_id for step in plan.steps) == (
        "disable-and-mark",
        "reenable",
    )
