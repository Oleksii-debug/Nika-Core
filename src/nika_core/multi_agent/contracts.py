from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from math import isfinite
from typing import Any
from uuid import uuid4

from nika_core.builder.spec import ToolGrant


class TeamState(StrEnum):
    ACTIVE = "active"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


class MemberState(StrEnum):
    SPAWNED = "spawned"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class HandoffKind(StrEnum):
    TASK = "task"
    RESULT = "result"
    STATUS = "status"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class TeamQuota:
    max_depth: int = 3
    max_children_per_parent: int = 4
    max_total_agents: int = 16
    max_parallel: int = 4

    def __post_init__(self) -> None:
        if self.max_depth < 1:
            raise ValueError("max_depth must be at least 1")
        if self.max_children_per_parent < 1:
            raise ValueError("max_children_per_parent must be at least 1")
        if self.max_total_agents < 2:
            raise ValueError("max_total_agents must be at least 2")
        if self.max_parallel < 1:
            raise ValueError("max_parallel must be at least 1")
        if self.max_parallel > self.max_total_agents:
            raise ValueError("max_parallel cannot exceed max_total_agents")


@dataclass(frozen=True, slots=True)
class TeamMember:
    team_id: str
    member_id: str
    parent_id: str | None
    depth: int
    agent_id: str
    agent_version: int
    thread_id: str
    tool_grants: tuple[ToolGrant, ...]
    state: MemberState = MemberState.SPAWNED
    resume_token: str | None = None


@dataclass(frozen=True, slots=True)
class StoredMemberResult:
    outcome: str
    payload: dict[str, Any]
    error: str | None = None

    def __post_init__(self) -> None:
        if not self.outcome.strip():
            raise ValueError("stored member result outcome must not be empty")


@dataclass(frozen=True, slots=True)
class AgentHandoff:
    team_id: str
    sender_id: str
    recipient_id: str
    kind: HandoffKind
    payload: dict[str, Any] = field(default_factory=dict)
    handoff_id: str = field(default_factory=lambda: uuid4().hex)
    correlation_id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self) -> None:
        if not self.team_id.strip() or not self.sender_id.strip() or not self.recipient_id.strip():
            raise ValueError("handoff identifiers must not be empty")


@dataclass(frozen=True, slots=True)
class ChildRequest:
    member_id: str
    agent_id: str
    agent_version: int
    thread_id: str
    requested_grants: tuple[ToolGrant, ...] = ()
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class EvaluationScore:
    evaluator_id: str
    target_member_id: str
    score: float
    metric: str = "quality"

    def __post_init__(self) -> None:
        if not self.evaluator_id.strip() or not self.target_member_id.strip():
            raise ValueError("evaluation identifiers must not be empty")
        if not self.metric.strip():
            raise ValueError("metric must not be empty")
        if not isfinite(float(self.score)):
            raise ValueError("evaluation score must be finite")


def attenuate_grants(
    parent_grants: tuple[ToolGrant, ...], requested_grants: tuple[ToolGrant, ...]
) -> tuple[ToolGrant, ...]:
    """Validate that a child requests a strict subset of the parent's privileges."""
    parent_by_tool = {grant.tool_id: grant for grant in parent_grants}
    result: list[ToolGrant] = []
    for requested in requested_grants:
        parent = parent_by_tool.get(requested.tool_id)
        if parent is None:
            raise PermissionError(f"child requested ungranted tool: {requested.tool_id}")
        if requested.max_risk > parent.max_risk:
            raise PermissionError(f"child requested higher risk for tool: {requested.tool_id}")
        if not set(requested.scopes).issubset(parent.scopes):
            raise PermissionError(f"child requested broader scope for tool: {requested.tool_id}")
        result.append(requested)
    return tuple(result)


def aggregate_scores(scores: tuple[EvaluationScore, ...]) -> dict[str, float]:
    metrics = {item.metric for item in scores}
    if len(metrics) > 1:
        raise ValueError("evaluation scores from multiple metrics must be aggregated separately")
    grouped: dict[str, list[float]] = {}
    for item in scores:
        grouped.setdefault(item.target_member_id, []).append(float(item.score))
    return {
        member_id: sum(values) / len(values)
        for member_id, values in sorted(grouped.items())
    }
