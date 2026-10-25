from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol
from unicodedata import normalize


class DeterministicErrorCode(StrEnum):
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    GOAL_UNREACHABLE = "goal_unreachable"
    NO_PLAN_FOUND = "no_plan_found"
    PLANNING_TIMEOUT = "planning_timeout"
    PLANNER_RESOURCE_LIMIT = "planner_resource_limit"
    UNSUPPORTED_PROBLEM = "unsupported_problem"
    PLANNER_FAILURE = "planner_failure"
    PLAN_TOO_LONG = "plan_too_long"
    INVALID_PLAN = "invalid_plan"
    REPLAN_LIMIT = "replan_limit"
    ACTION_UNAVAILABLE = "action_unavailable"
    STATE_OBSERVATION_TIMEOUT = "state_observation_timeout"
    STATE_OBSERVATION_FAILED = "state_observation_failed"
    TOOL_EXECUTION_FAILED = "tool_execution_failed"
    SIDE_EFFECT_JOURNAL_REQUIRED = "side_effect_journal_required"
    SIDE_EFFECT_IDENTITY_CONFLICT = "side_effect_identity_conflict"
    SIDE_EFFECT_RECONCILIATION_REQUIRED = "side_effect_reconciliation_required"
    SIDE_EFFECT_RECORD_FAILED = "side_effect_record_failed"
    GOAL_UNSATISFIED = "goal_unsatisfied"


class DeterministicPlanningError(RuntimeError):
    """Raised when an explicit-state goal cannot be planned safely."""

    def __init__(
        self,
        message: str,
        *,
        code: DeterministicErrorCode = DeterministicErrorCode.PLANNER_FAILURE,
    ) -> None:
        super().__init__(message)
        self.code = code


def _canonical_identifier(value: object, *, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    canonical = normalize("NFC", value).strip()
    if not canonical:
        raise ValueError(f"{field_name} must not be empty")
    return canonical


def _canonical_facts(
    values: object,
    *,
    field_name: str,
    reject_duplicates: bool,
) -> frozenset[str]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise TypeError(f"{field_name} must be an iterable of strings")

    canonical: set[str] = set()
    for value in values:
        fact = _canonical_identifier(value, field_name=f"{field_name} fact")
        if reject_duplicates and fact in canonical:
            raise ValueError(f"duplicate {field_name} constraint: {fact}")
        canonical.add(fact)
    return frozenset(canonical)


@dataclass(frozen=True, slots=True)
class WorldState:
    facts: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "facts",
            _canonical_facts(
                self.facts,
                field_name="state",
                reject_duplicates=False,
            ),
        )


@dataclass(frozen=True, slots=True)
class DeterministicGoal:
    required: frozenset[str] = field(default_factory=frozenset)
    forbidden: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        required = _canonical_facts(
            self.required,
            field_name="goal required",
            reject_duplicates=True,
        )
        forbidden = _canonical_facts(
            self.forbidden,
            field_name="goal forbidden",
            reject_duplicates=True,
        )
        if required & forbidden:
            raise ValueError("goal cannot require and forbid the same fact")
        object.__setattr__(self, "required", required)
        object.__setattr__(self, "forbidden", forbidden)


@dataclass(frozen=True, slots=True)
class DeterministicAction:
    action_id: str
    requires: frozenset[str] = field(default_factory=frozenset)
    forbids: frozenset[str] = field(default_factory=frozenset)
    adds: frozenset[str] = field(default_factory=frozenset)
    removes: frozenset[str] = field(default_factory=frozenset)
    tool_id: str | None = None
    arguments: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        action_id = _canonical_identifier(self.action_id, field_name="action_id")
        requires = _canonical_facts(
            self.requires,
            field_name="action requires",
            reject_duplicates=True,
        )
        forbids = _canonical_facts(
            self.forbids,
            field_name="action forbids",
            reject_duplicates=True,
        )
        adds = _canonical_facts(
            self.adds,
            field_name="action adds",
            reject_duplicates=True,
        )
        removes = _canonical_facts(
            self.removes,
            field_name="action removes",
            reject_duplicates=True,
        )
        if requires & forbids:
            raise ValueError("action cannot require and forbid the same fact")
        if adds & removes:
            raise ValueError("action cannot add and remove the same fact")
        if self.tool_id is None:
            tool_id = None
        else:
            tool_id = _canonical_identifier(self.tool_id, field_name="tool_id")
        if not isinstance(self.arguments, dict):
            raise TypeError("action arguments must be a dictionary")
        if any(not isinstance(key, str) for key in self.arguments):
            raise TypeError("action argument names must be strings")

        object.__setattr__(self, "action_id", action_id)
        object.__setattr__(self, "requires", requires)
        object.__setattr__(self, "forbids", forbids)
        object.__setattr__(self, "adds", adds)
        object.__setattr__(self, "removes", removes)
        object.__setattr__(self, "tool_id", tool_id)
        object.__setattr__(self, "arguments", dict(self.arguments))


def canonicalize_planner_inputs(
    *,
    state: WorldState,
    goal: DeterministicGoal,
    actions: tuple[DeterministicAction, ...],
) -> tuple[WorldState, DeterministicGoal, tuple[DeterministicAction, ...]]:
    """Validate and order Nika-owned symbolic inputs before deterministic planning."""
    if not isinstance(state, WorldState):
        raise TypeError("planner state must be WorldState")
    if not isinstance(goal, DeterministicGoal):
        raise TypeError("planner goal must be DeterministicGoal")
    if not goal.required and not goal.forbidden:
        raise ValueError("planner goal must contain at least one constraint")
    if isinstance(actions, (str, bytes)) or not isinstance(actions, Iterable):
        raise TypeError("planner capabilities must be an iterable of DeterministicAction records")

    capability_records = tuple(actions)
    if any(not isinstance(action, DeterministicAction) for action in capability_records):
        raise TypeError("planner capability records must be DeterministicAction instances")

    action_ids = [action.action_id for action in capability_records]
    if len(set(action_ids)) != len(action_ids):
        raise ValueError("duplicate deterministic action_id")

    return state, goal, tuple(sorted(capability_records, key=lambda action: action.action_id))


@dataclass(frozen=True, slots=True)
class PlanStep:
    action_id: str
    tool_id: str | None = None


@dataclass(frozen=True, slots=True)
class DeterministicPlan:
    steps: tuple[PlanStep, ...]


class DeterministicEffectStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class DeterministicEffectReservation:
    operation_key: str
    status: DeterministicEffectStatus
    created: bool


class DeterministicEffectConflictError(RuntimeError):
    """Raised when a durable effect identity is rebound to different action semantics."""


class DeterministicEffectJournal(Protocol):
    """Durable fail-closed journal for non-read-only deterministic tool effects."""

    def unresolved_operation_keys(self, *, task_id: str) -> tuple[str, ...]: ...

    def reserve(
        self,
        *,
        task_id: str,
        action: DeterministicAction,
    ) -> DeterministicEffectReservation: ...

    def complete(self, operation_key: str) -> None: ...

    def mark_uncertain(self, operation_key: str) -> None: ...

    def release_pending(self, operation_key: str) -> None: ...


class DeterministicPlanner(Protocol):
    def plan(
        self,
        *,
        state: WorldState,
        goal: DeterministicGoal,
        actions: tuple[DeterministicAction, ...],
    ) -> DeterministicPlan: ...


class WorldStateObserver(Protocol):
    async def observe(self) -> WorldState: ...
