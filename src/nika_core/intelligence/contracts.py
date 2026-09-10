from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping, Protocol


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


@dataclass(frozen=True, slots=True)
class WorldState:
    facts: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if any(not fact.strip() for fact in self.facts):
            raise ValueError("state facts must not be empty")


@dataclass(frozen=True, slots=True)
class DeterministicGoal:
    required: frozenset[str] = field(default_factory=frozenset)
    forbidden: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if self.required & self.forbidden:
            raise ValueError("goal cannot require and forbid the same fact")
        if any(not fact.strip() for fact in self.required | self.forbidden):
            raise ValueError("goal facts must not be empty")


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
        if not self.action_id.strip():
            raise ValueError("action_id must not be empty")
        if self.requires & self.forbids:
            raise ValueError("action cannot require and forbid the same fact")
        if self.adds & self.removes:
            raise ValueError("action cannot add and remove the same fact")
        facts = self.requires | self.forbids | self.adds | self.removes
        if any(not fact.strip() for fact in facts):
            raise ValueError("action facts must not be empty")
        if self.tool_id is not None and not self.tool_id.strip():
            raise ValueError("tool_id must not be empty")


@dataclass(frozen=True, slots=True)
class PlanStep:
    action_id: str
    tool_id: str | None = None


@dataclass(frozen=True, slots=True)
class DeterministicPlanProvenance:
    """Privacy-safe, durable evidence binding one deterministic plan to its context."""

    schema_version: int
    goal_fingerprint: str
    state_fingerprint: str
    state_version: str
    selected_rules_fingerprint: str
    selected_rule_count: int
    planner_id: str
    planner_version: str
    planner_strategy: str
    steps_fingerprint: str
    step_count: int
    planner_invoked: bool = True

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported deterministic plan provenance schema_version")
        for name in (
            "goal_fingerprint",
            "state_fingerprint",
            "selected_rules_fingerprint",
            "steps_fingerprint",
        ):
            value = getattr(self, name)
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError(f"{name} must be a lowercase SHA-256 fingerprint")
        for name in ("state_version", "planner_id", "planner_version", "planner_strategy"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must not be empty")
            if len(value) > 256 or any(ord(char) < 32 for char in value):
                raise ValueError(f"{name} is invalid")
        if self.selected_rule_count < 0 or self.step_count < 0:
            raise ValueError("deterministic plan provenance counts must be non-negative")
        if type(self.planner_invoked) is not bool:
            raise TypeError("planner_invoked must be a boolean")

    def to_dict(self) -> dict[str, object]:
        """Return only JSON-safe evidence suitable for the existing audit/checkpoint stores."""

        return {
            "schema_version": self.schema_version,
            "goal_fingerprint": self.goal_fingerprint,
            "state_fingerprint": self.state_fingerprint,
            "state_version": self.state_version,
            "selected_rules_fingerprint": self.selected_rules_fingerprint,
            "selected_rule_count": self.selected_rule_count,
            "planner_id": self.planner_id,
            "planner_version": self.planner_version,
            "planner_strategy": self.planner_strategy,
            "steps_fingerprint": self.steps_fingerprint,
            "step_count": self.step_count,
            "planner_invoked": self.planner_invoked,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> DeterministicPlanProvenance:
        """Restore provenance from durable JSON without accepting partial/stale-shaped data."""

        expected = {
            "schema_version",
            "goal_fingerprint",
            "state_fingerprint",
            "state_version",
            "selected_rules_fingerprint",
            "selected_rule_count",
            "planner_id",
            "planner_version",
            "planner_strategy",
            "steps_fingerprint",
            "step_count",
            "planner_invoked",
        }
        if set(payload) != expected:
            raise ValueError("deterministic plan provenance fields do not match schema v1")

        schema_version = payload["schema_version"]
        selected_rule_count = payload["selected_rule_count"]
        step_count = payload["step_count"]
        planner_invoked = payload["planner_invoked"]
        if type(schema_version) is not int:
            raise TypeError("schema_version must be an integer")
        if type(selected_rule_count) is not int or type(step_count) is not int:
            raise TypeError("deterministic plan provenance counts must be integers")
        if type(planner_invoked) is not bool:
            raise TypeError("planner_invoked must be a boolean")

        text_fields = {
            name: payload[name]
            for name in (
                "goal_fingerprint",
                "state_fingerprint",
                "state_version",
                "selected_rules_fingerprint",
                "planner_id",
                "planner_version",
                "planner_strategy",
                "steps_fingerprint",
            )
        }
        if any(not isinstance(value, str) for value in text_fields.values()):
            raise TypeError("deterministic plan provenance text fields must be strings")

        return cls(
            schema_version=schema_version,
            goal_fingerprint=str(text_fields["goal_fingerprint"]),
            state_fingerprint=str(text_fields["state_fingerprint"]),
            state_version=str(text_fields["state_version"]),
            selected_rules_fingerprint=str(text_fields["selected_rules_fingerprint"]),
            selected_rule_count=selected_rule_count,
            planner_id=str(text_fields["planner_id"]),
            planner_version=str(text_fields["planner_version"]),
            planner_strategy=str(text_fields["planner_strategy"]),
            steps_fingerprint=str(text_fields["steps_fingerprint"]),
            step_count=step_count,
            planner_invoked=planner_invoked,
        )


@dataclass(frozen=True, slots=True)
class DeterministicPlan:
    steps: tuple[PlanStep, ...]
    provenance: DeterministicPlanProvenance | None = None


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
