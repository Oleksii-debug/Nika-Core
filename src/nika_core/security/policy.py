from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from nika_core.tools import ToolRisk


@dataclass(frozen=True, slots=True)
class SandboxPolicy:
    workspace_root: Path
    writable_roots: tuple[str, ...] = ()
    allowed_network_hosts: tuple[str, ...] = ()
    allowed_executables: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        root = self.workspace_root.resolve()
        object.__setattr__(self, "workspace_root", root)
        for relative in self.writable_roots:
            path = Path(relative)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("writable roots must be workspace-relative")

    def resolve_write(self, relative_path: str) -> Path:
        candidate_path = Path(relative_path)
        if candidate_path.is_absolute() or ".." in candidate_path.parts:
            raise PermissionError("write path must be workspace-relative")
        candidate = (self.workspace_root / candidate_path).resolve()
        if candidate != self.workspace_root and self.workspace_root not in candidate.parents:
            raise PermissionError("write path escapes workspace")
        if not self.writable_roots:
            raise PermissionError("filesystem writes are disabled")
        allowed = tuple((self.workspace_root / item).resolve() for item in self.writable_roots)
        if not any(candidate == root or root in candidate.parents for root in allowed):
            raise PermissionError("write path is outside allowed roots")
        return candidate

    def authorize_network(self, host: str) -> None:
        normalized = host.strip().lower().rstrip(".")
        allowed = {item.strip().lower().rstrip(".") for item in self.allowed_network_hosts}
        if not normalized or normalized not in allowed:
            raise PermissionError("network host is not allowed")

    def authorize_executable(self, executable: str) -> None:
        name = Path(executable).name.casefold()
        allowed = {Path(item).name.casefold() for item in self.allowed_executables}
        if not name or name not in allowed:
            raise PermissionError("process executable is not allowed")


@dataclass(frozen=True, slots=True)
class ExecutionBudget:
    max_write_bytes: int = 0
    max_network_calls: int = 0
    max_process_launches: int = 0

    def __post_init__(self) -> None:
        if min(self.max_write_bytes, self.max_network_calls, self.max_process_launches) < 0:
            raise ValueError("execution budgets must be non-negative")


@dataclass(slots=True)
class ExecutionBudgetLedger:
    budget: ExecutionBudget
    write_bytes: int = 0
    network_calls: int = 0
    process_launches: int = 0

    def reserve(self, intent: ActionIntent) -> None:
        next_write = self.write_bytes + intent.write_bytes
        next_network = self.network_calls + int(intent.network_host is not None)
        next_process = self.process_launches + int(intent.executable is not None)
        if next_write > self.budget.max_write_bytes:
            raise PermissionError("filesystem write budget exceeded")
        if next_network > self.budget.max_network_calls:
            raise PermissionError("network call budget exceeded")
        if next_process > self.budget.max_process_launches:
            raise PermissionError("process launch budget exceeded")
        self.write_bytes = next_write
        self.network_calls = next_network
        self.process_launches = next_process


@dataclass(frozen=True, slots=True)
class ActionIntent:
    action_id: str
    tool_id: str
    risk: ToolRisk
    target: str
    write_path: str | None = None
    write_bytes: int = 0
    network_host: str | None = None
    executable: str | None = None
    approval_required: bool = False

    def __post_init__(self) -> None:
        if not self.action_id.strip() or not self.tool_id.strip() or not self.target.strip():
            raise ValueError("action identity fields must not be empty")
        if self.write_bytes < 0:
            raise ValueError("write_bytes must be non-negative")
        if self.write_bytes and self.write_path is None:
            raise ValueError("write_bytes require write_path")

    @property
    def approval_fingerprint(self) -> str:
        payload = "\x1f".join(
            (
                self.action_id,
                self.tool_id,
                self.risk.value,
                self.target,
                self.write_path or "",
                str(self.write_bytes),
                self.network_host or "",
                self.executable or "",
            )
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ApprovalEvidence:
    approval_id: str
    action_fingerprint: str
    approved_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        if not self.approval_id.strip() or not self.action_fingerprint.strip():
            raise ValueError("approval identity must not be empty")
        if self.approved_at.tzinfo is None or self.expires_at.tzinfo is None:
            raise ValueError("approval timestamps must be timezone-aware")
        if self.expires_at <= self.approved_at:
            raise ValueError("approval expiry must follow approval time")


@dataclass(slots=True)
class ApprovalLedger:
    _used: set[str] = field(default_factory=set)

    def consume(
        self,
        intent: ActionIntent,
        approval: ApprovalEvidence | None,
        *,
        now: datetime | None = None,
    ) -> None:
        if approval is None:
            raise PermissionError("explicit approval is required")
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            raise ValueError("current time must be timezone-aware")
        if approval.approval_id in self._used:
            raise PermissionError("approval evidence was already used")
        if approval.action_fingerprint != intent.approval_fingerprint:
            raise PermissionError("approval does not match the exact action")
        if current < approval.approved_at or current >= approval.expires_at:
            raise PermissionError("approval is not currently valid")
        self._used.add(approval.approval_id)


@dataclass(frozen=True, slots=True)
class SecurityPolicy:
    granted_tools: frozenset[str]
    sandbox: SandboxPolicy
    budget: ExecutionBudget


@dataclass(frozen=True, slots=True)
class SecurityDecision:
    action_id: str
    approved: bool
    resolved_write_path: Path | None = None


def authorize_action(
    intent: ActionIntent,
    policy: SecurityPolicy,
    budgets: ExecutionBudgetLedger,
    approvals: ApprovalLedger,
    *,
    approval: ApprovalEvidence | None = None,
    now: datetime | None = None,
) -> SecurityDecision:
    """Defense-in-depth authorization before an action reaches an external adapter."""
    if intent.tool_id not in policy.granted_tools:
        raise PermissionError("tool is not granted by downstream policy")

    resolved_write: Path | None = None
    if intent.write_path is not None:
        resolved_write = policy.sandbox.resolve_write(intent.write_path)
    if intent.network_host is not None:
        policy.sandbox.authorize_network(intent.network_host)
    if intent.executable is not None:
        policy.sandbox.authorize_executable(intent.executable)

    requires_approval = intent.approval_required or intent.risk is ToolRisk.HIGH_IMPACT
    if requires_approval:
        approvals.consume(intent, approval, now=now)

    budgets.reserve(intent)
    return SecurityDecision(
        action_id=intent.action_id,
        approved=True,
        resolved_write_path=resolved_write,
    )
