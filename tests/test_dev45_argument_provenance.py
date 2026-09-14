from __future__ import annotations

import json

import pytest

from nika_core.intelligence.contracts import (
    DeterministicAction,
    DeterministicGoal,
    DeterministicPlan,
    PlanStep,
    WorldState,
)
from nika_core.intelligence.plan_provenance import (
    DeterministicPlanProvenanceMismatchError,
    seal_plan_provenance,
    verify_plan_provenance,
)


class _Planner:
    planner_id = "argument-binding-test"
    planner_version = "1"
    planner_strategy = "fixed"


def _plan() -> DeterministicPlan:
    return DeterministicPlan(steps=(PlanStep(action_id="send", tool_id="tool.send"),))


def _action(arguments: dict[str, object]) -> DeterministicAction:
    return DeterministicAction(
        action_id="send",
        tool_id="tool.send",
        arguments=arguments,
    )


def test_argument_only_semantic_change_invalidates_provenance_without_persisting_raw_values() -> None:
    state = WorldState()
    goal = DeterministicGoal(required=frozenset({"sent"}))
    original = _action(
        {
            "recipient_private": "alice@example.invalid",
            "payload": {"secret_text": "gamma-secret"},
        }
    )
    sealed = seal_plan_provenance(
        _plan(),
        state=state,
        goal=goal,
        actions=(original,),
        planner=_Planner(),
    )
    assert sealed.provenance is not None

    durable_json = json.dumps(sealed.provenance.to_dict(), sort_keys=True)
    assert "recipient_private" not in durable_json
    assert "alice@example.invalid" not in durable_json
    assert "secret_text" not in durable_json
    assert "gamma-secret" not in durable_json

    changed = _action(
        {
            "recipient_private": "mallory@example.invalid",
            "payload": {"secret_text": "gamma-secret"},
        }
    )
    with pytest.raises(
        DeterministicPlanProvenanceMismatchError,
        match="does not match the exact plan context",
    ):
        verify_plan_provenance(
            sealed,
            state=state,
            goal=goal,
            actions=(changed,),
            planner=_Planner(),
        )


def test_action_arguments_are_a_defensive_frozen_snapshot_for_provenance_and_execution() -> None:
    source = {"payload": {"token": "original"}}
    action = _action(source)

    source["payload"]["token"] = "mutated-before-execution"  # type: ignore[index]
    first_execution_copy = dict(action.arguments)
    assert first_execution_copy == {"payload": {"token": "original"}}

    nested = first_execution_copy["payload"]
    assert isinstance(nested, dict)
    nested["token"] = "mutated-copy"
    assert dict(action.arguments) == {"payload": {"token": "original"}}

    with pytest.raises(TypeError):
        action.arguments["new"] = "value"  # type: ignore[index]
