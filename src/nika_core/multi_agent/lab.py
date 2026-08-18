from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID, uuid4

from nika_core.agents.spec import PermissionPolicy, RiskLevel, ToolGrant


class MessageKind(StrEnum):
    TASK = "task"
    RESULT = "result"
    ERROR = "error"
    STATUS = "status"


@dataclass(frozen=True, slots=True)
class AgentMessage:
    """Typed parent/child message with explicit provenance."""

    sender_id: str
    recipient_id: str
    kind: MessageKind
    payload: str
    correlation_id: UUID = field(default_factory=uuid4)


@dataclass(frozen=True, slots=True)
class DelegationQuota:
    """Hard bounds for one multi-agent laboratory tree."""

    max_depth: int = 3
    max_children_per_parent: int = 4
    max_total_agents: int = 16

    def __post_init__(self) -> None:
        if self.max_depth < 1:
            raise ValueError("max_depth must be at least 1")
        if self.max_children_per_parent < 1:
            raise ValueError("max_children_per_parent must be at least 1")
        if self.max_total_agents < 2:
            raise ValueError("max_total_agents must be at least 2")


@dataclass(frozen=True, slots=True)
class AgentNode:
    agent_id: str
    parent_id: str | None
    depth: int
    policy: PermissionPolicy


def attenuate_policy(parent: PermissionPolicy, requested: PermissionPolicy) -> PermissionPolicy:
    """Return a child policy that can only reduce the parent's privileges."""

    parent_grants = {grant.tool_id: grant for grant in parent.tool_grants}
    grants: list[ToolGrant] = []
    for requested_grant in requested.tool_grants:
        parent_grant = parent_grants.get(requested_grant.tool_id)
        if parent_grant is None:
            continue
        max_risk = RiskLevel(min(int(parent_grant.max_risk), int(requested_grant.max_risk)))
        parent_scopes = set(parent_grant.scopes)
        scopes = tuple(scope for scope in requested_grant.scopes if scope in parent_scopes)
        grants.append(ToolGrant(tool_id=requested_grant.tool_id, max_risk=max_risk, scopes=scopes))

    return PermissionPolicy(
        default_risk=RiskLevel(min(int(parent.default_risk), int(requested.default_risk))),
        tool_grants=tuple(grants),
        allow_network=parent.allow_network and requested.allow_network,
        allow_filesystem_write=parent.allow_filesystem_write and requested.allow_filesystem_write,
        allow_process_launch=parent.allow_process_launch and requested.allow_process_launch,
    )


class MultiAgentLab:
    """Deterministic delegation ledger; runtime execution remains behind M1-M4 ports."""

    def __init__(self, root_id: str, root_policy: PermissionPolicy, quota: DelegationQuota | None = None) -> None:
        self.quota = quota or DelegationQuota()
        self._nodes: dict[str, AgentNode] = {
            root_id: AgentNode(root_id, None, 0, root_policy),
        }
        self._children: dict[str, list[str]] = {root_id: []}
        self._messages: list[AgentMessage] = []

    @property
    def nodes(self) -> tuple[AgentNode, ...]:
        return tuple(self._nodes.values())

    @property
    def messages(self) -> tuple[AgentMessage, ...]:
        return tuple(self._messages)

    def delegate(self, parent_id: str, child_id: str, requested_policy: PermissionPolicy) -> AgentNode:
        if child_id in self._nodes:
            raise ValueError(f"agent already exists: {child_id}")
        try:
            parent = self._nodes[parent_id]
        except KeyError as exc:
            raise KeyError(f"unknown parent agent: {parent_id}") from exc
        if parent.depth + 1 > self.quota.max_depth:
            raise RuntimeError("delegation depth quota exceeded")
        children = self._children.setdefault(parent_id, [])
        if len(children) >= self.quota.max_children_per_parent:
            raise RuntimeError("children-per-parent quota exceeded")
        if len(self._nodes) >= self.quota.max_total_agents:
            raise RuntimeError("total-agent quota exceeded")

        child = AgentNode(
            agent_id=child_id,
            parent_id=parent_id,
            depth=parent.depth + 1,
            policy=attenuate_policy(parent.policy, requested_policy),
        )
        self._nodes[child_id] = child
        children.append(child_id)
        self._children[child_id] = []
        return child

    def record_message(self, message: AgentMessage) -> None:
        if message.sender_id not in self._nodes:
            raise KeyError(f"unknown sender agent: {message.sender_id}")
        if message.recipient_id not in self._nodes:
            raise KeyError(f"unknown recipient agent: {message.recipient_id}")
        self._messages.append(message)

    def ancestry(self, agent_id: str) -> tuple[str, ...]:
        try:
            node = self._nodes[agent_id]
        except KeyError as exc:
            raise KeyError(f"unknown agent: {agent_id}") from exc
        lineage: list[str] = []
        while node.parent_id is not None:
            lineage.append(node.parent_id)
            node = self._nodes[node.parent_id]
        return tuple(lineage)
