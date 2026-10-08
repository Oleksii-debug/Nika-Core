"""Plan 5 Trader PAPER read admission through the existing Core permission store.

The host alone supplies a StandingPermissionBinding. Browser/agent command
payloads can never select a permission, subject, context, workspace or run.
This adapter does not grant permissions or own a second policy authority.
"""
from __future__ import annotations

from collections.abc import Callable

from nika_core.security.policy import ActionIntent
from nika_core.security.standing_permission import (
    PermissionContext,
    StandingPermissionBinding,
    StandingPermissionStore,
    StandingPermissionUse,
)
from nika_core.tools import ToolRisk

from .paper_actions import PAPER_ACCOUNT_INSPECT, PAPER_POSITIONS_INSPECT


def standing_paper_read_authorizer(
    permissions: StandingPermissionStore,
    *,
    trusted_binding: Callable[[], StandingPermissionBinding],
    action_id: str = PAPER_ACCOUNT_INSPECT,
) -> Callable[[str, str], bool]:
    """Compose PaperWorkspaceQuery with Core's durable, revocable READ_ONLY grant.

    The binding must come from authenticated host context on *each* call. Its
    target is the exact workspace_id and its resource_id is the exact run_id.
    The action must be bound by the host: inspecting detailed PAPER positions
    cannot borrow the narrower account-summary grant.
    Core also matches subject, user/project/task context, action class, risk,
    expiry, parent grants, and revocation. Absent/invalid authority fails
    closed; nothing here creates a standing permission.
    """
    if type(permissions) is not StandingPermissionStore:
        raise TypeError("canonical standing permission store is required")
    if not callable(trusted_binding):
        raise TypeError("trusted host binding resolver is required")
    if type(action_id) is not str or action_id not in (
        PAPER_ACCOUNT_INSPECT, PAPER_POSITIONS_INSPECT
    ):
        raise ValueError("unsupported PAPER read action")

    def authorize(workspace_id: str, run_id: str) -> bool:
        if type(workspace_id) is not str or type(run_id) is not str:
            return False
        try:
            binding = trusted_binding()
            if type(binding) is not StandingPermissionBinding:
                return False
            context = binding.context
            if (
                type(context) is not PermissionContext
                or type(context.user_id) is not str
                or type(context.project_id) is not str
                or type(context.task_id) is not str
                or type(binding.permission_id) is not str
                or type(binding.subject_id) is not str
                or type(binding.target) is not str
                or type(binding.resource_id) is not str
                or binding.target != workspace_id
                or binding.resource_id != run_id
                or binding.network_host is not None
            ):
                return False

            use = StandingPermissionUse(
                subject_id=binding.subject_id,
                context=context,
                intent=ActionIntent(
                    action_id=action_id,
                    tool_id=action_id,
                    target=workspace_id,
                    risk=ToolRisk.READ_ONLY,
                ),
                resource_id=run_id,
            )
            permissions.authorize(binding.permission_id, use)
            return True
        except Exception:
            # Permission integrity, expiry, revoke, storage/host faults and
            # behavioral data must not reveal account state or exception text.
            return False

    return authorize
