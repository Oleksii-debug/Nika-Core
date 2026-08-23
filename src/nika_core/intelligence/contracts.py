from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


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
    TOOL_EXECUTION_FAILED = "tool_execution_failed"
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
class DeterministicPlan:
    steps: tuple[PlanStep, ...]


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
