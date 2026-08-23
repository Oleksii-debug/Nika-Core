from __future__ import annotations

import asyncio
from dataclasses import dataclass

from nika_core.intelligence.contracts import (
    DeterministicAction,
    DeterministicEffectConflictError,
    DeterministicEffectJournal,
    DeterministicEffectStatus,
    DeterministicErrorCode,
    DeterministicGoal,
    DeterministicPlan,
    DeterministicPlanner,
    DeterministicPlanningError,
    WorldState,
    WorldStateObserver,
)
from nika_core.tools import ToolCall, ToolExecutor, ToolRisk, ToolSpec


@dataclass(frozen=True, slots=True)
class DeterministicBrainResult:
    plan: DeterministicPlan
    completed_actions: tuple[str, ...]
    final_state: WorldState
    error: str | None = None
    error_code: DeterministicErrorCode | None = None
    planning_history: tuple[DeterministicPlan, ...] = ()
    replans: int = 0

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass(frozen=True, slots=True)
class _PlanValidationFailure:
    code: DeterministicErrorCode
    message: str


@dataclass(frozen=True, slots=True)
class _ToolExecutionFailure:
    code: DeterministicErrorCode
    message: str


class DeterministicBrain:
    """Plan, validate, re-plan and execute explicit workflows without a language model."""

    def __init__(
        self,
        *,
        planner: DeterministicPlanner,
        tools: ToolExecutor,
        effect_journal: DeterministicEffectJournal | None = None,
    ) -> None:
        self._planner = planner
        self._tools = tools
        self._effect_journal = effect_journal

    async def run(
        self,
        *,
        run_id: str,
        state: WorldState,
        goal: DeterministicGoal,
        actions: tuple[DeterministicAction, ...],
        approved_action_ids: frozenset[str] = frozenset(),
        previously_completed_action_ids: tuple[str, ...] = (),
        state_observer: WorldStateObserver | None = None,
        task_id: str | None = None,
        max_steps: int = 100,
        max_replans: int = 8,
        planning_timeout_seconds: float = 30.0,
    ) -> DeterministicBrainResult:
        if not run_id.strip():
            raise ValueError("run_id must not be empty")
        if max_steps <= 0:
            raise ValueError("max_steps must be greater than zero")
        if max_replans < 0:
            raise ValueError("max_replans must be non-negative")
        if planning_timeout_seconds <= 0:
            raise ValueError("planning_timeout_seconds must be greater than zero")
        if self._effect_journal is not None and (task_id is None or not task_id.strip()):
            raise ValueError("task_id is required when effect_journal is configured")

        # Kept for source compatibility only. A planner-selected action ID is not approval
        # evidence and must never turn into ToolCall.approved=True.
        del approved_action_ids

        action_map = {action.action_id: action for action in actions}
        if len(action_map) != len(actions):
            raise ValueError("duplicate deterministic action_id")
        if len(set(previously_completed_action_ids)) != len(previously_completed_action_ids):
            raise ValueError("duplicate previously completed deterministic action_id")
        unknown_completed = [
            action_id
            for action_id in previously_completed_action_ids
            if action_id not in action_map
        ]
        if unknown_completed:
            raise ValueError(
                f"previously completed deterministic action is unavailable: {unknown_completed[0]}"
            )

        tool_specs = {spec.tool_id: spec for spec in self._tools.specs()}
        current_state = state
        completed = list(previously_completed_action_ids)
        completed_set = set(previously_completed_action_ids)
        history: list[DeterministicPlan] = []
        replans = 0
        executed_steps = 0
        loop = asyncio.get_running_loop()
        planning_deadline = loop.time() + planning_timeout_seconds

        while True:
            remaining_steps = max_steps - executed_steps
            if remaining_steps <= 0:
                return self._failure(
                    plan=history[-1] if history else DeterministicPlan(steps=()),
                    completed=completed,
                    state=current_state,
                    history=history,
                    replans=replans,
                    code=DeterministicErrorCode.PLAN_TOO_LONG,
                    message=f"execution reached max_steps budget: {max_steps}",
                )

            available_actions = tuple(
                action for action in actions if action.action_id not in completed_set
            )
            plan = await self._plan(
                state=current_state,
                goal=goal,
                actions=available_actions,
                planning_deadline=planning_deadline,
            )
            history.append(plan)

            validation_failure = self._validate_plan(
                plan=plan,
                state=current_state,
                goal=goal,
                action_map=action_map,
                completed_action_ids=completed_set,
                remaining_steps=remaining_steps,
            )
            if validation_failure is not None:
                return self._failure(
                    plan=plan,
                    completed=completed,
                    state=current_state,
                    history=history,
                    replans=replans,
                    code=validation_failure.code,
                    message=validation_failure.message,
                )

            replan_requested = False
            for index, step in enumerate(plan.steps):
                if state_observer is not None:
                    observed = await state_observer.observe()
                    if observed != current_state:
                        current_state = observed
                        if self._goal_satisfied(current_state, goal):
                            return DeterministicBrainResult(
                                plan=plan,
                                completed_actions=tuple(completed),
                                final_state=current_state,
                                planning_history=tuple(history),
                                replans=replans,
                            )
                        replans += 1
                        if replans > max_replans:
                            return self._failure(
                                plan=plan,
                                completed=completed,
                                state=current_state,
                                history=history,
                                replans=replans,
                                code=DeterministicErrorCode.REPLAN_LIMIT,
                                message=(
                                    "changed world state exceeded max_replans budget: "
                                    f"{max_replans}"
                                ),
                            )
                        replan_requested = True
                        break

                action = action_map.get(step.action_id)
                if action is None or action.action_id in completed_set:
                    return self._failure(
                        plan=plan,
                        completed=completed,
                        state=current_state,
                        history=history,
                        replans=replans,
                        code=DeterministicErrorCode.ACTION_UNAVAILABLE,
                        message=f"planned action is unavailable: {step.action_id}",
                    )
                if not self._action_applicable(action, current_state):
                    replans += 1
                    if replans > max_replans:
                        return self._failure(
                            plan=plan,
                            completed=completed,
                            state=current_state,
                            history=history,
                            replans=replans,
                            code=DeterministicErrorCode.REPLAN_LIMIT,
                            message=(
                                "changed world state exceeded max_replans budget: "
                                f"{max_replans}"
                            ),
                        )
                    replan_requested = True
                    break

                tool_failure = await self._execute_tool_action(
                    action=action,
                    run_id=run_id,
                    task_id=task_id,
                    executed_steps=executed_steps,
                    plan_index=index,
                    tool_specs=tool_specs,
                )
                if tool_failure is not None:
                    return self._failure(
                        plan=plan,
                        completed=completed,
                        state=current_state,
                        history=history,
                        replans=replans,
                        code=tool_failure.code,
                        message=tool_failure.message,
                    )

                current_state = self._apply(action, current_state)
                completed.append(action.action_id)
                completed_set.add(action.action_id)
                executed_steps += 1

            if replan_requested:
                continue

            if state_observer is not None:
                observed = await state_observer.observe()
                if observed != current_state:
                    current_state = observed
                    if not self._goal_satisfied(current_state, goal):
                        replans += 1
                        if replans > max_replans:
                            return self._failure(
                                plan=plan,
                                completed=completed,
                                state=current_state,
                                history=history,
                                replans=replans,
                                code=DeterministicErrorCode.REPLAN_LIMIT,
                                message=(
                                    "changed world state exceeded max_replans budget: "
                                    f"{max_replans}"
                                ),
                            )
                        continue

            if self._goal_satisfied(current_state, goal):
                return DeterministicBrainResult(
                    plan=plan,
                    completed_actions=tuple(completed),
                    final_state=current_state,
                    planning_history=tuple(history),
                    replans=replans,
                )
            return self._failure(
                plan=plan,
                completed=completed,
                state=current_state,
                history=history,
                replans=replans,
                code=DeterministicErrorCode.GOAL_UNSATISFIED,
                message="plan completed without satisfying the goal",
            )

    async def _execute_tool_action(
        self,
        *,
        action: DeterministicAction,
        run_id: str,
        task_id: str | None,
        executed_steps: int,
        plan_index: int,
        tool_specs: dict[str, ToolSpec],
    ) -> _ToolExecutionFailure | None:
        if action.tool_id is None:
            return None

        spec = tool_specs.get(action.tool_id)
        journal = self._effect_journal
        if spec is None or spec.risk == ToolRisk.READ_ONLY:
            return await self._execute_tool_call(
                action=action,
                call_id=f"{run_id}:{executed_steps}:{plan_index}:{action.action_id}",
            )
        if journal is None:
            return _ToolExecutionFailure(
                DeterministicErrorCode.SIDE_EFFECT_JOURNAL_REQUIRED,
                "non-read-only deterministic tool requires a durable effect journal",
            )
        if task_id is None:  # validated at run entry
            raise AssertionError("durable deterministic task identity is unavailable")

        try:
            reservation = journal.reserve(
                task_id=task_id,
                action=action,
            )
        except DeterministicEffectConflictError as exc:
            return _ToolExecutionFailure(
                DeterministicErrorCode.SIDE_EFFECT_IDENTITY_CONFLICT,
                str(exc),
            )
        except Exception as exc:  # noqa: BLE001 - fail closed before the external effect.
            return _ToolExecutionFailure(
                DeterministicErrorCode.SIDE_EFFECT_RECORD_FAILED,
                f"could not reserve deterministic tool effect: {type(exc).__name__}",
            )

        if not reservation.created:
            if reservation.status == DeterministicEffectStatus.COMPLETED:
                return None
            return _ToolExecutionFailure(
                DeterministicErrorCode.SIDE_EFFECT_RECONCILIATION_REQUIRED,
                "deterministic tool effect is pending or uncertain and requires reconciliation",
            )

        try:
            result = await self._tools.execute(
                ToolCall(
                    call_id=reservation.operation_key,
                    tool_id=action.tool_id,
                    arguments=dict(action.arguments),
                    approved=False,
                )
            )
        except asyncio.CancelledError:
            self._mark_effect_uncertain_best_effort(journal, reservation.operation_key)
            raise
        except BaseException:
            # Abrupt process loss deliberately leaves the durable reservation PENDING. Startup
            # recovery treats PENDING exactly like UNCERTAIN and refuses automatic replay.
            raise

        if not result.ok:
            if result.error in {"approval required", "unknown tool"}:
                try:
                    journal.release_pending(reservation.operation_key)
                except Exception as exc:  # noqa: BLE001 - do not hide a broken durable ledger.
                    return _ToolExecutionFailure(
                        DeterministicErrorCode.SIDE_EFFECT_RECORD_FAILED,
                        f"could not release unused effect reservation: {type(exc).__name__}",
                    )
                return _ToolExecutionFailure(
                    DeterministicErrorCode.TOOL_EXECUTION_FAILED,
                    result.error or "tool failed",
                )

            try:
                journal.mark_uncertain(reservation.operation_key)
            except Exception as exc:  # noqa: BLE001 - PENDING still blocks replay fail-closed.
                return _ToolExecutionFailure(
                    DeterministicErrorCode.SIDE_EFFECT_RECORD_FAILED,
                    f"tool failed and durable uncertainty could not be recorded: {type(exc).__name__}",
                )
            return _ToolExecutionFailure(
                DeterministicErrorCode.SIDE_EFFECT_RECONCILIATION_REQUIRED,
                result.error or "tool effect failed with uncertain external outcome",
            )

        try:
            journal.complete(reservation.operation_key)
        except Exception as exc:  # noqa: BLE001 - effect happened; reservation remains fail-closed.
            return _ToolExecutionFailure(
                DeterministicErrorCode.SIDE_EFFECT_RECORD_FAILED,
                f"tool effect succeeded but durable completion record failed: {type(exc).__name__}",
            )
        return None

    async def _execute_tool_call(
        self,
        *,
        action: DeterministicAction,
        call_id: str,
    ) -> _ToolExecutionFailure | None:
        if action.tool_id is None:
            return None
        result = await self._tools.execute(
            ToolCall(
                call_id=call_id,
                tool_id=action.tool_id,
                arguments=dict(action.arguments),
                approved=False,
            )
        )
        if result.ok:
            return None
        return _ToolExecutionFailure(
            DeterministicErrorCode.TOOL_EXECUTION_FAILED,
            result.error or "tool failed",
        )

    @staticmethod
    def _mark_effect_uncertain_best_effort(
        journal: DeterministicEffectJournal,
        operation_key: str,
    ) -> None:
        try:
            journal.mark_uncertain(operation_key)
        except Exception:  # noqa: BLE001 - PENDING reservation still blocks automatic replay.
            return

    async def _plan(
        self,
        *,
        state: WorldState,
        goal: DeterministicGoal,
        actions: tuple[DeterministicAction, ...],
        planning_deadline: float,
    ) -> DeterministicPlan:
        loop = asyncio.get_running_loop()
        remaining = planning_deadline - loop.time()
        if remaining <= 0:
            raise DeterministicPlanningError(
                "deterministic planner timed out",
                code=DeterministicErrorCode.PLANNING_TIMEOUT,
            )
        try:
            async with asyncio.timeout(remaining):
                return await asyncio.to_thread(
                    self._planner.plan,
                    state=state,
                    goal=goal,
                    actions=actions,
                )
        except TimeoutError as exc:
            raise DeterministicPlanningError(
                "deterministic planner timed out",
                code=DeterministicErrorCode.PLANNING_TIMEOUT,
            ) from exc

    @classmethod
    def _validate_plan(
        cls,
        *,
        plan: DeterministicPlan,
        state: WorldState,
        goal: DeterministicGoal,
        action_map: dict[str, DeterministicAction],
        completed_action_ids: set[str],
        remaining_steps: int,
    ) -> _PlanValidationFailure | None:
        if cls._goal_satisfied(state, goal):
            if plan.steps:
                return _PlanValidationFailure(
                    DeterministicErrorCode.INVALID_PLAN,
                    "planner returned actions although the declared goal is already satisfied",
                )
            return None

        if len(plan.steps) > remaining_steps:
            return _PlanValidationFailure(
                DeterministicErrorCode.PLAN_TOO_LONG,
                f"plan exceeds max_steps budget: {len(plan.steps)} > {remaining_steps}",
            )

        simulated = state
        seen: set[str] = set()
        for step in plan.steps:
            action = action_map.get(step.action_id)
            if action is None:
                return _PlanValidationFailure(
                    DeterministicErrorCode.INVALID_PLAN,
                    f"planned action is unavailable: {step.action_id}",
                )
            if action.action_id in completed_action_ids or action.action_id in seen:
                return _PlanValidationFailure(
                    DeterministicErrorCode.INVALID_PLAN,
                    f"plan repeats a completed deterministic action: {action.action_id}",
                )
            if step.tool_id != action.tool_id:
                return _PlanValidationFailure(
                    DeterministicErrorCode.INVALID_PLAN,
                    f"planned tool identity does not match action: {action.action_id}",
                )
            if not cls._action_applicable(action, simulated):
                return _PlanValidationFailure(
                    DeterministicErrorCode.INVALID_PLAN,
                    f"planned action preconditions are not true: {action.action_id}",
                )
            next_state = cls._apply(action, simulated)
            if next_state == simulated:
                return _PlanValidationFailure(
                    DeterministicErrorCode.INVALID_PLAN,
                    f"planned action has no new deterministic effect: {action.action_id}",
                )
            simulated = next_state
            seen.add(action.action_id)

        if not cls._goal_satisfied(simulated, goal):
            return _PlanValidationFailure(
                DeterministicErrorCode.INVALID_PLAN,
                "planner returned a plan that does not satisfy the declared goal",
            )
        return None

    @staticmethod
    def _action_applicable(action: DeterministicAction, state: WorldState) -> bool:
        return action.requires <= state.facts and not action.forbids & state.facts

    @staticmethod
    def _apply(action: DeterministicAction, state: WorldState) -> WorldState:
        facts = set(state.facts)
        facts.difference_update(action.removes)
        facts.update(action.adds)
        return WorldState(frozenset(facts))

    @staticmethod
    def _goal_satisfied(state: WorldState, goal: DeterministicGoal) -> bool:
        return goal.required <= state.facts and not goal.forbidden & state.facts

    @staticmethod
    def _failure(
        *,
        plan: DeterministicPlan,
        completed: list[str],
        state: WorldState,
        history: list[DeterministicPlan],
        replans: int,
        code: DeterministicErrorCode,
        message: str,
    ) -> DeterministicBrainResult:
        return DeterministicBrainResult(
            plan=plan,
            completed_actions=tuple(completed),
            final_state=state,
            error=message,
            error_code=code,
            planning_history=tuple(history),
            replans=replans,
        )
