from __future__ import annotations

from typing import Any

from nika_core.intelligence.contracts import (
    DeterministicAction,
    DeterministicErrorCode,
    DeterministicGoal,
    DeterministicPlan,
    DeterministicPlanningError,
    PlanStep,
    WorldState,
)


class UnifiedPlanningAdapter:
    """Adapter from Nika boolean-fact planning contracts to Unified Planning."""

    def __init__(self, *, engine_name: str = "aries") -> None:
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
        if self._goal_satisfied(state, goal):
            return DeterministicPlan(steps=())

        unreachable_fact = self._obviously_unreachable_fact(state, goal, actions)
        if unreachable_fact is not None:
            raise DeterministicPlanningError(
                f"goal fact is unreachable from registered deterministic actions: {unreachable_fact}",
                code=DeterministicErrorCode.GOAL_UNREACHABLE,
            )

        try:
            up = self._shortcuts()
        except (ImportError, ModuleNotFoundError) as exc:
            raise DeterministicPlanningError(
                "Unified Planning is not installed; install the 'planning' optional component",
                code=DeterministicErrorCode.DEPENDENCY_UNAVAILABLE,
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

        try:
            with up.OneshotPlanner(name=self._engine_name) as planner:
                result = planner.solve(problem)
        except Exception as exc:
            raise DeterministicPlanningError(
                "deterministic planner failed",
                code=DeterministicErrorCode.PLANNER_FAILURE,
            ) from exc

        status_name = getattr(result.status, "name", str(result.status))
        if result.plan is None:
            raise self._result_error(status_name)

        if status_name not in {"SOLVED_SATISFICING", "SOLVED_OPTIMALLY"}:
            raise DeterministicPlanningError(
                f"deterministic planner returned inconsistent status: {status_name}",
                code=DeterministicErrorCode.PLANNER_FAILURE,
            )

        actions_in_plan = getattr(result.plan, "actions", None)
        if actions_in_plan is None:
            raise DeterministicPlanningError(
                "deterministic planner returned a non-sequential plan",
                code=DeterministicErrorCode.UNSUPPORTED_PROBLEM,
            )

        plan_steps: list[PlanStep] = []
        for action_instance in actions_in_plan:
            definition = action_by_up_name.get(action_instance.action.name)
            if definition is None:
                raise DeterministicPlanningError(
                    "planner returned an unknown action",
                    code=DeterministicErrorCode.PLANNER_FAILURE,
                )
            plan_steps.append(
                PlanStep(action_id=definition.action_id, tool_id=definition.tool_id)
            )
        return DeterministicPlan(steps=tuple(plan_steps))

    @staticmethod
    def _result_error(status_name: str) -> DeterministicPlanningError:
        if status_name == "UNSOLVABLE_PROVEN":
            return DeterministicPlanningError(
                "goal is not reachable from the current state",
                code=DeterministicErrorCode.GOAL_UNREACHABLE,
            )
        if status_name == "UNSOLVABLE_INCOMPLETELY":
            return DeterministicPlanningError(
                "planner could not find a plan for the current state and goal",
                code=DeterministicErrorCode.NO_PLAN_FOUND,
            )
        if status_name == "TIMEOUT":
            return DeterministicPlanningError(
                "deterministic planner timed out",
                code=DeterministicErrorCode.PLANNING_TIMEOUT,
            )
        if status_name == "MEMOUT":
            return DeterministicPlanningError(
                "deterministic planner exhausted its memory budget",
                code=DeterministicErrorCode.PLANNER_RESOURCE_LIMIT,
            )
        if status_name == "UNSUPPORTED_PROBLEM":
            return DeterministicPlanningError(
                "deterministic planner does not support this planning problem",
                code=DeterministicErrorCode.UNSUPPORTED_PROBLEM,
            )
        return DeterministicPlanningError(
            f"deterministic planner failed with status: {status_name}",
            code=DeterministicErrorCode.PLANNER_FAILURE,
        )

    @staticmethod
    def _obviously_unreachable_fact(
        state: WorldState,
        goal: DeterministicGoal,
        actions: tuple[DeterministicAction, ...],
    ) -> str | None:
        addable: set[str] = set()
        removable: set[str] = set()
        for action in actions:
            addable.update(action.adds)
            removable.update(action.removes)

        missing_required = goal.required - state.facts
        unreachable_required = sorted(missing_required - addable)
        if unreachable_required:
            return unreachable_required[0]

        present_forbidden = goal.forbidden & state.facts
        unreachable_forbidden = sorted(present_forbidden - removable)
        if unreachable_forbidden:
            return unreachable_forbidden[0]
        return None

    @staticmethod
    def _goal_satisfied(state: WorldState, goal: DeterministicGoal) -> bool:
        return goal.required <= state.facts and not goal.forbidden & state.facts

    @staticmethod
    def _shortcuts() -> Any:
        from unified_planning import shortcuts

        return shortcuts
