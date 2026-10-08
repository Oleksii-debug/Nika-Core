from __future__ import annotations

import asyncio
import math
import unicodedata
from copy import deepcopy
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
    PlanStep,
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


@dataclass(frozen=True, slots=True)
class _StateObservationFailure:
    code: DeterministicErrorCode
    message: str


def _require_run_identity(value: object, *, name: str) -> None:
    """Reuse canonical bounded UTF-8 identity admission before durable effects."""
    if type(value) is not str or not value or len(value) > 512 or value != value.strip():
        raise ValueError(f"{name} must be canonical bounded UTF-8 text")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{name} must be canonical bounded UTF-8 text")
    if any(
        unicodedata.category(character) in {"Cc", "Cf", "Zl", "Zp"}
        for character in value
    ):
        raise ValueError(f"{name} must be canonical bounded UTF-8 text")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must be canonical bounded UTF-8 text") from exc
    if len(encoded) > 512:
        raise ValueError(f"{name} must be canonical bounded UTF-8 text")


def _positive_finite_seconds(value: object, *, name: str) -> float:
    """Admit an exact finite deadline budget before starting work."""
    if type(value) not in (int, float):
        raise ValueError(f"{name} must be a positive finite number")
    try:
        seconds = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} must be a positive finite number") from exc
    if not math.isfinite(seconds):
        raise ValueError(f"{name} must be a positive finite number")
    if seconds <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return seconds


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
        observation_timeout_seconds: float = 10.0,
    ) -> DeterministicBrainResult:
        _require_run_identity(run_id, name="run_id")
        if task_id is not None:
            _require_run_identity(task_id, name="task_id")
        if type(max_steps) is not int or max_steps <= 0:
            raise ValueError("max_steps must be a positive integer")
        if type(max_replans) is not int or max_replans < 0:
            raise ValueError("max_replans must be a non-negative integer")
        planning_timeout_seconds = _positive_finite_seconds(
            planning_timeout_seconds, name="planning_timeout_seconds"
        )
        observation_timeout_seconds = _positive_finite_seconds(
            observation_timeout_seconds, name="observation_timeout_seconds"
        )
        if self._effect_journal is not None and (task_id is None or not task_id.strip()):
            raise ValueError("task_id is required when effect_journal is configured")

        # Kept for source compatibility only. A planner-selected action ID is not approval
        # evidence and must never turn into ToolCall.approved=True.
        del approved_action_ids

        # Recovery checkpoints and the action catalog are immutable sequences. A
        # consumable iterator could pass the first identity scan and then appear
        # empty when calculating the remaining step budget, replaying completed work.
        if type(actions) is not tuple:
            raise ValueError("actions must be an immutable tuple")
        if type(previously_completed_action_ids) is not tuple:
            raise ValueError("previously_completed_action_ids must be an immutable tuple")

        # Treat caller-owned records as input, not mutable runtime authority. In
        # particular, a frozen DeterministicAction still contains a mutable arguments
        # mapping. A planner or state observer must not be able to change that mapping
        # between plan validation, durable reservation and ToolExecutor dispatch.
        if type(state) is not WorldState or type(goal) is not DeterministicGoal:
            raise ValueError("state and goal must be canonical deterministic records")
        if any(type(action) is not DeterministicAction for action in actions):
            raise ValueError("actions must be canonical deterministic action records")
        try:
            state, goal, actions = deepcopy((state, goal, actions))
        except Exception as exc:
            raise ValueError("deterministic run inputs cannot be detached safely") from exc

        # Action IDs are durable completion/effect generation identities.
        # Admit all of them before planner or journal operations.
        for action in actions:
            _require_run_identity(action.action_id, name="action_id")
        for action_id in previously_completed_action_ids:
            _require_run_identity(action_id, name="previously_completed_action_id")
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
        # A recovered action already consumed this task's total execution budget.
        executed_steps = len(previously_completed_action_ids)

        journal = self._effect_journal
        if journal is not None:
            if task_id is None:  # validated at run entry
                raise AssertionError("durable deterministic task identity is unavailable")
            try:
                unresolved = journal.unresolved_operation_keys(task_id=task_id)
            except Exception as exc:  # noqa: BLE001 - fail closed before planning or effects.
                return self._failure(
                    plan=DeterministicPlan(steps=()),
                    completed=completed,
                    state=current_state,
                    history=history,
                    replans=replans,
                    code=DeterministicErrorCode.SIDE_EFFECT_RECORD_FAILED,
                    message=(
                        "could not inspect deterministic task effect state: "
                        f"{type(exc).__name__}"
                    ),
                )
            if unresolved:
                return self._failure(
                    plan=DeterministicPlan(steps=()),
                    completed=completed,
                    state=current_state,
                    history=history,
                    replans=replans,
                    code=DeterministicErrorCode.SIDE_EFFECT_RECONCILIATION_REQUIRED,
                    message=(
                        "deterministic task has an unresolved side effect and requires "
                        f"reconciliation: {unresolved[0]}"
                    ),
                )

        # A checkpoint with an already-satisfied goal is terminal irrespective of
        # how much budget remains. Never plan an unnecessary external effect just
        # because a prior run stopped short of the step ceiling.
        # Task-wide unresolved journal records above still take precedence.
        if executed_steps <= max_steps and self._goal_satisfied(current_state, goal):
            # The caller's recovered state may be stale. Re-observe it before
            # claiming success when an authoritative observer is configured.
            if state_observer is not None:
                observed, observation_failure = await self._observe_state(
                    state_observer, timeout_seconds=observation_timeout_seconds
                )
                if observation_failure is not None:
                    return self._failure(
                        plan=DeterministicPlan(steps=()),
                        completed=completed,
                        state=current_state,
                        history=history,
                        replans=replans,
                        code=observation_failure.code,
                        message=observation_failure.message,
                    )
                if observed is None:  # pragma: no cover - observer contract
                    raise AssertionError("state observation returned no state or failure")
                current_state = observed
                if not self._goal_satisfied(current_state, goal):
                    if executed_steps == max_steps:
                        return self._failure(
                            plan=DeterministicPlan(steps=()),
                            completed=completed,
                            state=current_state,
                            history=history,
                            replans=replans,
                            code=DeterministicErrorCode.PLAN_TOO_LONG,
                            message="recovered state changed after max_steps was exhausted",
                        )
                    # Observed drift invalidated terminality. Continue through
                    # the normal validated planner using the remaining budget.
                else:
                    return DeterministicBrainResult(
                        plan=DeterministicPlan(steps=()),
                        completed_actions=tuple(completed),
                        final_state=current_state,
                        planning_history=tuple(history),
                        replans=0,
                    )
            else:
                return DeterministicBrainResult(
                    plan=DeterministicPlan(steps=()),
                    completed_actions=tuple(completed),
                    final_state=current_state,
                    planning_history=tuple(history),
                    replans=0,
                )

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
            # The planner is a replaceable/untrusted adapter. Validate its carrier
            # before indexing a step or publishing it as durable plan evidence.
            # Malformed steps must never reach ToolExecutor or a journal reservation.
            if (
                type(plan) is not DeterministicPlan
                or type(plan.steps) is not tuple
                or any(
                    type(step) is not PlanStep
                    or type(step.action_id) is not str
                    or (step.tool_id is not None and type(step.tool_id) is not str)
                    for step in plan.steps
                )
            ):
                return self._failure(
                    plan=DeterministicPlan(steps=()),
                    completed=completed,
                    state=current_state,
                    history=history,
                    replans=replans,
                    code=DeterministicErrorCode.INVALID_PLAN,
                    message="planner returned a malformed deterministic plan",
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
                    observed, observation_failure = await self._observe_state(
                        state_observer,
                        timeout_seconds=observation_timeout_seconds,
                    )
                    if observation_failure is not None:
                        return self._failure(
                            plan=plan,
                            completed=completed,
                            state=current_state,
                            history=history,
                            replans=replans,
                            code=observation_failure.code,
                            message=observation_failure.message,
                        )
                    if observed is None:  # pragma: no cover - helper invariant
                        raise AssertionError("state observation returned no state or failure")
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
                observed, observation_failure = await self._observe_state(
                    state_observer,
                    timeout_seconds=observation_timeout_seconds,
                )
                if observation_failure is not None:
                    return self._failure(
                        plan=plan,
                        completed=completed,
                        state=current_state,
                        history=history,
                        replans=replans,
                        code=observation_failure.code,
                        message=observation_failure.message,
                    )
                if observed is None:  # pragma: no cover - helper invariant
                    raise AssertionError("state observation returned no state or failure")
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

        if spec.risk in {ToolRisk.EXTERNAL_SIDE_EFFECT, ToolRisk.HIGH_IMPACT}:
            return await self._execute_external_tool_action(
                action=action,
                task_id=task_id,
            )

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

    async def _execute_external_tool_action(
        self,
        *,
        action: DeterministicAction,
        task_id: str,
    ) -> _ToolExecutionFailure | None:
        """Delegate one external effect to ToolExecutor's canonical durable guard."""
        if action.tool_id is None:  # pragma: no cover - caller invariant
            return None
        result = await self._tools.execute(
            ToolCall(
                call_id=f"deterministic:{action.action_id}",
                tool_id=action.tool_id,
                arguments=dict(action.arguments),
                approved=False,
                task_id=task_id,
            )
        )
        if result.ok:
            return None
        if result.error in {
            "tool effect not safe to execute",
            "tool timed out",
            "tool failed",
            "tool result durability failed",
        }:
            return _ToolExecutionFailure(
                DeterministicErrorCode.SIDE_EFFECT_RECONCILIATION_REQUIRED,
                result.error,
            )
        return _ToolExecutionFailure(
            DeterministicErrorCode.TOOL_EXECUTION_FAILED,
            result.error or "tool failed",
        )

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

    @staticmethod
    async def _observe_state(
        observer: WorldStateObserver,
        *,
        timeout_seconds: float,
    ) -> tuple[WorldState | None, _StateObservationFailure | None]:
        try:
            async with asyncio.timeout(timeout_seconds):
                observed = await observer.observe()
        except TimeoutError:
            return None, _StateObservationFailure(
                DeterministicErrorCode.STATE_OBSERVATION_TIMEOUT,
                "world-state observation timed out",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize observer adapter failures.
            return None, _StateObservationFailure(
                DeterministicErrorCode.STATE_OBSERVATION_FAILED,
                f"world-state observation failed: {type(exc).__name__}",
            )
        if not isinstance(observed, WorldState):
            return None, _StateObservationFailure(
                DeterministicErrorCode.STATE_OBSERVATION_FAILED,
                "world-state observer returned an invalid state type",
            )
        return observed, None

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
            # Planner implementations are replaceable and run in another thread.
            # Give them detached candidates rather than live validation/permission
            # state: even object.__setattr__ can mutate frozen dataclass carriers.
            planner_state, planner_goal, planner_actions = deepcopy((state, goal, actions))
            async with asyncio.timeout(remaining):
                return await asyncio.to_thread(
                    self._planner.plan,
                    state=planner_state,
                    goal=planner_goal,
                    actions=planner_actions,
                )
        except TimeoutError as exc:
            raise DeterministicPlanningError(
                "deterministic planner timed out",
                code=DeterministicErrorCode.PLANNING_TIMEOUT,
            ) from exc
        except DeterministicPlanningError:
            raise
        except Exception as exc:
            # Replaceable planner failures must not leak provider-specific details
            # or cross the model-free execution boundary as arbitrary exceptions.
            raise DeterministicPlanningError(
                "deterministic planner adapter failed",
                code=DeterministicErrorCode.PLANNER_FAILURE,
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
