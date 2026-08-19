from __future__ import annotations

from typing import Any

from nika_core.intelligence.contracts import (
    DeterministicAction,
    DeterministicGoal,
    DeterministicPlan,
    DeterministicPlanningError,
    PlanStep,
    WorldState,
)


class UnifiedPlanningAdapter:
    """Adapter from Nika boolean-fact planning contracts to Unified Planning."""

    def __init__(self, *, engine_name: str = "pyperplan") -> None:
        if not engine_name.strip():
            raise ValueError("engine_name must not be empty")
        self._engine_name = engine_name

    def plan(
        self,
        *,
        state: WorldState,
        goal: DeterministicGoal,
        actions: tuple[DeterministicAction, ...],
    ) -> DeterministicPlan:
        try:
            up = self._shortcuts()
        except (ImportError, ModuleNotFoundError) as exc:
            raise DeterministicPlanningError(
                "Unified Planning is not installed; install the 'planning' optional component"
            ) from exc

        all_facts = set(state.facts) | set(goal.required) | set(goal.forbidden)
        for action in actions:
            all_facts.update(action.requires)
            all_facts.update(action.forbids)
            all_facts.update(action.adds)
            all_facts.update(action.removes)

        problem = up.Problem("nika-deterministic-plan")
        fluents: dict[str, Any] = {}
        for index, fact in enumerate(sorted(all_facts)):
            fluent = up.Fluent(f"fact_{index}", up.BoolType())
            fluents[fact] = fluent
            problem.add_fluent(fluent, default_initial_value=False)
            problem.set_initial_value(fluent, fact in state.facts)

        action_by_up_name: dict[str, DeterministicAction] = {}
        for index, definition in enumerate(actions):
            up_name = f"action_{index}"
            planned_action = up.InstantaneousAction(up_name)
            for fact in definition.requires:
                planned_action.add_precondition(fluents[fact])
            for fact in definition.forbids:
                planned_action.add_precondition(up.Not(fluents[fact]))
            for fact in definition.adds:
                planned_action.add_effect(fluents[fact], True)
            for fact in definition.removes:
                planned_action.add_effect(fluents[fact], False)
            problem.add_action(planned_action)
            action_by_up_name[up_name] = definition

        for fact in goal.required:
            problem.add_goal(fluents[fact])
        for fact in goal.forbidden:
            problem.add_goal(up.Not(fluents[fact]))

        if not goal.required and not goal.forbidden:
            return DeterministicPlan(steps=())

        try:
            with up.OneshotPlanner(name=self._engine_name) as planner:
                result = planner.solve(problem)
        except Exception as exc:
            raise DeterministicPlanningError("deterministic planner failed") from exc

        if result.plan is None:
            raise DeterministicPlanningError("goal is not reachable from the current state")

        plan_steps: list[PlanStep] = []
        for action_instance in result.plan.actions:
            definition = action_by_up_name.get(action_instance.action.name)
            if definition is None:
                raise DeterministicPlanningError("planner returned an unknown action")
            plan_steps.append(
                PlanStep(action_id=definition.action_id, tool_id=definition.tool_id)
            )
        return DeterministicPlan(steps=tuple(plan_steps))

    @staticmethod
    def _shortcuts() -> Any:
        from unified_planning import shortcuts

        return shortcuts
