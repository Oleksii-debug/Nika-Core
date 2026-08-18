from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from nika_core.agents.spec import PermissionPolicy, RiskLevel


@dataclass(frozen=True, slots=True)
class ActionIntent:
    tool_id: str
    risk: RiskLevel
    target_path: str | None = None
    requires_network: bool = False
    launches_process: bool = False


@dataclass(frozen=True, slots=True)
class HumanApproval:
    action_id: str
    approved: bool
    token: str


class DownstreamGuard:
    """Fail-closed downstream authorization before a tool invocation reaches M1-M4."""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()

    def authorize(
        self,
        intent: ActionIntent,
        policy: PermissionPolicy,
        approval: HumanApproval | None = None,
    ) -> None:
        if intent.risk >= RiskLevel.R4_EXPLICIT_HUMAN:
            if approval is None or not approval.approved or not approval.token.strip():
                raise PermissionError("R4 action requires explicit human approval")
        elif not policy.permits(intent.tool_id, intent.risk):
            raise PermissionError(f"tool is not granted: {intent.tool_id}")

        if intent.requires_network and not policy.allow_network:
            raise PermissionError("network access is not granted")
        if intent.launches_process and not policy.allow_process_launch:
            raise PermissionError("process launch is not granted")
        if intent.target_path is not None:
            if not policy.allow_filesystem_write and intent.risk >= RiskLevel.R1_REVERSIBLE:
                raise PermissionError("filesystem write is not granted")
            target = (self.workspace_root / intent.target_path).resolve()
            if target != self.workspace_root and self.workspace_root not in target.parents:
                raise PermissionError("target path escapes workspace")
