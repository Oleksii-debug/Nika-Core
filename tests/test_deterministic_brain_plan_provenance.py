from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.intelligence.brain import DeterministicBrain
from nika_core.intelligence.contracts import (
    DeterministicAction,
    DeterministicGoal,
    DeterministicPlan,
    DeterministicPlanProvenance,
    PlanStep,
    WorldState,
)
from nika_core.intelligence.plan_provenance import (
    DeterministicPlanProvenanceMismatchError,
    seal_plan_provenance,
    verify_plan_provenance,
)
from nika_core.intelligence.unified_planning_adapter import UnifiedPlanningAdapter
from nika_core.kernel.audit import AuditLog
from nika_core.tools import ToolExecutor


class _FixedPlanner:
    planner_id = "test-fixed-planner"
    planner_version = "7"
    planner_strategy = "fixed-sequential"

    def __init__(self) -> None:
        self.returned_provenance: DeterministicPlanProvenance | None = None

    def plan(
        self,
        *,
        state: WorldState,
        goal: DeterministicGoal,
        actions: tuple[DeterministicAction, ...],
    ) -> DeterministicPlan:
        del state, goal, actions
        return DeterministicPlan(
            steps=(PlanStep(action_id="advance"),),
            provenance=self.returned_provenance,
        )


def _context() -> tuple[WorldState, DeterministicGoal, tuple[DeterministicAction, ...]]:
    state = WorldState(frozenset({"private-state-value:alpha-secret"}))
    goal = DeterministicGoal(required=frozenset({"private-goal-value:beta-secret"}))
    actions = (
        DeterministicAction(
            action_id="advance",
            requires=frozenset({"private-state-value:alpha-secret"}),
            adds=frozenset({"private-goal-value:beta-secret"}),
            arguments={"credential_like_input": "gamma-secret"},
        ),
    )
    return state, goal, actions


def _run(planner: _FixedPlanner):
    state, goal, actions = _context()
    result = asyncio.run(
        DeterministicBrain(planner=planner, tools=ToolExecutor()).run(
            run_id="plan-provenance",
            state=state,
            goal=goal,
            actions=actions,
        )
    )
    assert result.ok
    return result, state, goal, actions


def test_plan_provenance_matches_exact_plan_without_persisting_private_inputs() -> None:
    planner = _FixedPlanner()
    result, state, goal, actions = _run(planner)

    provenance = result.plan.provenance
    assert provenance is not None
    assert provenance.schema_version == 1
    assert provenance.state_version == "world-state/v1"
    assert provenance.selected_rule_count == 1
    assert provenance.step_count == 1
    assert provenance.planner_id == "test-fixed-planner"
    assert provenance.planner_version == "7"
    assert provenance.planner_strategy == "fixed-sequential"
    assert provenance.planner_invoked is True
    verify_plan_provenance(
        result.plan,
        state=state,
        goal=goal,
        actions=actions,
        planner=planner,
    )

    durable_json = json.dumps(provenance.to_dict(), sort_keys=True)
    assert "alpha-secret" not in durable_json
    assert "beta-secret" not in durable_json
    assert "gamma-secret" not in durable_json
    assert "private-state-value" not in durable_json
    assert "private-goal-value" not in durable_json
    assert "credential_like_input" not in durable_json


def test_brain_reseals_spoofed_or_stale_planner_provenance() -> None:
    planner = _FixedPlanner()
    stale_state = WorldState(frozenset({"stale-state"}))
    stale_goal = DeterministicGoal(required=frozenset({"stale-goal"}))
    stale_action = DeterministicAction(
        action_id="advance",
        requires=frozenset({"stale-state"}),
        adds=frozenset({"stale-goal"}),
    )
    stale = seal_plan_provenance(
        DeterministicPlan(steps=(PlanStep(action_id="advance"),)),
        state=stale_state,
        goal=stale_goal,
        actions=(stale_action,),
        planner=planner,
    )
    assert stale.provenance is not None
    planner.returned_provenance = stale.provenance

    result, state, goal, actions = _run(planner)

    assert result.plan.provenance is not None
    assert result.plan.provenance != stale.provenance
    assert result.planning_history == (result.plan,)
    verify_plan_provenance(
        result.plan,
        state=state,
        goal=goal,
        actions=actions,
        planner=planner,
    )
    with pytest.raises(
        DeterministicPlanProvenanceMismatchError,
        match="does not match the exact plan context",
    ):
        verify_plan_provenance(
            replace(result.plan, provenance=stale.provenance),
            state=state,
            goal=goal,
            actions=actions,
            planner=planner,
        )
    with pytest.raises(
        DeterministicPlanProvenanceMismatchError,
        match="does not match the exact plan context",
    ):
        verify_plan_provenance(
            replace(
                result.plan,
                steps=(PlanStep(action_id="advance", tool_id="substituted-tool"),),
            ),
            state=state,
            goal=goal,
            actions=actions,
            planner=planner,
        )


def test_plan_provenance_round_trips_through_existing_audit_and_rejects_stale_restart_context(
    tmp_path,
) -> None:
    planner = _FixedPlanner()
    result, state, goal, actions = _run(planner)
    provenance = result.plan.provenance
    assert provenance is not None

    path = tmp_path / "nika.db"
    store = SQLiteStore(path)
    store.initialize()
    AuditLog(store).append(
        event_type="deterministic.plan.produced",
        entity_type="task",
        entity_id="task-plan-provenance",
        payload={"plan_provenance": provenance.to_dict()},
    )

    restarted_store = SQLiteStore(path)
    restarted_store.initialize()
    events = AuditLog(restarted_store).list_for(
        entity_type="task",
        entity_id="task-plan-provenance",
    )
    assert len(events) == 1
    durable_payload = events[0].payload["plan_provenance"]
    assert isinstance(durable_payload, dict)
    restored = DeterministicPlanProvenance.from_dict(durable_payload)
    restored_plan = replace(result.plan, provenance=restored)
    verify_plan_provenance(
        restored_plan,
        state=state,
        goal=goal,
        actions=actions,
        planner=planner,
    )

    with pytest.raises(DeterministicPlanProvenanceMismatchError):
        verify_plan_provenance(
            restored_plan,
            state=WorldState(frozenset({"new-state-after-restart"})),
            goal=goal,
            actions=actions,
            planner=planner,
        )
    with pytest.raises(DeterministicPlanProvenanceMismatchError):
        verify_plan_provenance(
            restored_plan,
            state=state,
            goal=goal,
            actions=(replace(actions[0], adds=frozenset({"different-rule"})),),
            planner=planner,
        )


def test_unified_planning_adapter_exposes_versioned_strategy_even_without_solver_invocation() -> None:
    planner = UnifiedPlanningAdapter(engine_name="aries")
    state = WorldState(frozenset({"done"}))
    goal = DeterministicGoal(required=frozenset({"done"}))

    plan = planner.plan(state=state, goal=goal, actions=())

    assert plan.steps == ()
    assert plan.provenance is not None
    assert plan.provenance.planner_id == "unified-planning"
    assert plan.provenance.planner_version == "nika-adapter/v1"
    assert plan.provenance.planner_strategy == "oneshot:aries"
    verify_plan_provenance(
        plan,
        state=state,
        goal=goal,
        actions=(),
        planner=planner,
    )


def test_fail_closed_preplanning_result_still_has_truthful_noninvoked_provenance() -> None:
    class BlockingJournal:
        def unresolved_operation_keys(self, *, task_id: str) -> tuple[str, ...]:
            assert task_id == "task-with-pending-effect"
            return ("deterministic:pending",)

    planner = _FixedPlanner()
    state, goal, actions = _context()
    result = asyncio.run(
        DeterministicBrain(
            planner=planner,
            tools=ToolExecutor(),
            effect_journal=BlockingJournal(),  # type: ignore[arg-type]
        ).run(
            run_id="preplanning-block",
            task_id="task-with-pending-effect",
            state=state,
            goal=goal,
            actions=actions,
        )
    )

    assert not result.ok
    assert result.plan.steps == ()
    assert result.plan.provenance is not None
    assert result.plan.provenance.planner_invoked is False
    verify_plan_provenance(
        result.plan,
        state=state,
        goal=goal,
        actions=actions,
        planner=planner,
        planner_invoked=False,
    )
