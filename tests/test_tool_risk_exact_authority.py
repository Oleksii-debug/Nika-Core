from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.security.policy import ActionIntent
from nika_core.security.standing_permission import (
    PermissionContext,
    StandingPermissionScope,
    StandingPermissionStore,
)
from nika_core.tools import ToolAuthorization, ToolRisk, ToolSpec


class _ForgedRisk(str):
    @property
    def value(self) -> str:
        return ToolRisk.HIGH_IMPACT.value


def _forged_read_only_risk() -> _ForgedRisk:
    return _ForgedRisk(ToolRisk.READ_ONLY.value)


def test_public_tool_authority_rejects_equality_compatible_foreign_risk_carriers() -> None:
    forged = _forged_read_only_risk()

    with pytest.raises(TypeError, match="exact ToolRisk"):
        ToolSpec(tool_id="safe.read", description="read", risk=forged)  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="exact ToolRisk"):
        ToolAuthorization(
            tool_id="safe.read",
            task_id="task-1",
            risk=forged,  # type: ignore[arg-type]
            arguments_fingerprint="a",
            effect_fingerprint="b",
            approval_fingerprint="c",
        )

    with pytest.raises(TypeError, match="exact ToolRisk"):
        ActionIntent(
            action_id="call-1",
            tool_id="safe.read",
            risk=forged,  # type: ignore[arg-type]
            target="target-1",
        )


def test_standing_permission_rejects_forged_risk_before_materialization() -> None:
    now = datetime(2026, 9, 27, tzinfo=UTC)

    with pytest.raises(TypeError, match="exact ToolRisk"):
        StandingPermissionScope(
            subject_id="agent-1",
            context=PermissionContext(
                user_id="user-1",
                project_id="project-1",
                task_id="task-1",
            ),
            action_class="safe.read",
            targets=("target-1",),
            sites=(),
            resources=("resource-1",),
            risk_ceiling=_forged_read_only_risk(),  # type: ignore[arg-type]
            granted_at=now,
            expires_at=now + timedelta(minutes=5),
        )


def test_forged_standing_risk_cannot_leave_durable_authority(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    permissions = StandingPermissionStore(store)
    permissions.initialize()
    now = datetime(2026, 9, 27, tzinfo=UTC)

    with pytest.raises(TypeError, match="exact ToolRisk"):
        scope = StandingPermissionScope(
            subject_id="agent-1",
            context=PermissionContext(
                user_id="user-1",
                project_id="project-1",
                task_id="task-1",
            ),
            action_class="safe.read",
            targets=("target-1",),
            sites=(),
            resources=("resource-1",),
            risk_ceiling=_forged_read_only_risk(),  # type: ignore[arg-type]
            granted_at=now,
            expires_at=now + timedelta(minutes=5),
        )
        permissions.grant(permission_id="forged-risk", scope=scope)

    with store.connection() as conn:
        row_count = conn.execute(
            "SELECT COUNT(*) AS count FROM standing_permissions"
        ).fetchone()["count"]
    assert row_count == 0


@pytest.mark.parametrize("risk", tuple(ToolRisk))
def test_exact_toolrisk_members_remain_valid_for_tool_and_intent_authority(
    risk: ToolRisk,
) -> None:
    spec = ToolSpec(tool_id="tool.action", description="action", risk=risk)
    intent = ActionIntent(
        action_id="call-1",
        tool_id=spec.tool_id,
        risk=risk,
        target="target-1",
    )
    authorization = ToolAuthorization(
        tool_id=spec.tool_id,
        task_id="task-1",
        risk=risk,
        arguments_fingerprint="a",
        effect_fingerprint="b",
        approval_fingerprint="c",
    )

    assert spec.risk is risk
    assert intent.risk is risk
    assert authorization.risk is risk


@pytest.mark.parametrize(
    "risk",
    (
        ToolRisk.READ_ONLY,
        ToolRisk.LOCAL_WRITE,
        ToolRisk.EXTERNAL_SIDE_EFFECT,
    ),
)
def test_exact_non_high_impact_risk_ceiling_remains_valid(risk: ToolRisk) -> None:
    now = datetime(2026, 9, 27, tzinfo=UTC)
    scope = StandingPermissionScope(
        subject_id="agent-1",
        context=PermissionContext(
            user_id="user-1",
            project_id="project-1",
            task_id="task-1",
        ),
        action_class="tool.action",
        targets=("target-1",),
        sites=(),
        resources=("resource-1",),
        risk_ceiling=risk,
        granted_at=now,
        expires_at=now + timedelta(minutes=5),
    )

    assert scope.risk_ceiling is risk
