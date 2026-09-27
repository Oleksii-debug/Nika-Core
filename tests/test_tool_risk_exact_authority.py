from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.security.policy import ActionIntent
from nika_core.security.standing_permission import (
    PermissionContext,
    StandingPermissionBinding,
    StandingPermissionScope,
    StandingPermissionStore,
    StandingPermissionUse,
)
from nika_core.tools import ToolRisk


class _ForgedRisk(str):
    @property
    def value(self) -> str:
        return ToolRisk.HIGH_IMPACT.value


class _MutableContext:
    def __init__(self) -> None:
        self.user_id = "user-1"
        self.project_id = "project-1"
        self.task_id = "task-1"


class _IntentCarrier:
    tool_id = "safe.read"
    target = "target-1"
    network_host = None
    risk = ToolRisk.READ_ONLY


def _forged_read_only_risk() -> _ForgedRisk:
    return _ForgedRisk(ToolRisk.READ_ONLY.value)


def _scope(*, risk_ceiling: ToolRisk) -> StandingPermissionScope:
    now = datetime(2026, 9, 27, tzinfo=UTC)
    return StandingPermissionScope(
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
        risk_ceiling=risk_ceiling,
        granted_at=now,
        expires_at=now + timedelta(minutes=5),
    )


def _grant_with_forged_risk(permissions: StandingPermissionStore) -> None:
    permissions.grant(
        permission_id="forged-risk",
        scope=_scope(risk_ceiling=_forged_read_only_risk()),  # type: ignore[arg-type]
    )


def test_nested_context_carriers_must_be_canonical_permission_context() -> None:
    fake_context = _MutableContext()
    now = datetime(2026, 9, 27, tzinfo=UTC)

    with pytest.raises(TypeError, match="exact PermissionContext"):
        StandingPermissionBinding(
            permission_id="perm-1",
            subject_id="agent-1",
            context=fake_context,  # type: ignore[arg-type]
            target="target-1",
            resource_id="resource-1",
            network_host=None,
        )

    with pytest.raises(TypeError, match="exact PermissionContext"):
        StandingPermissionScope(
            subject_id="agent-1",
            context=fake_context,  # type: ignore[arg-type]
            action_class="safe.read",
            targets=("target-1",),
            sites=(),
            resources=("resource-1",),
            risk_ceiling=ToolRisk.READ_ONLY,
            granted_at=now,
            expires_at=now + timedelta(minutes=5),
        )

    intent = ActionIntent(
        action_id="call-1",
        tool_id="safe.read",
        risk=ToolRisk.READ_ONLY,
        target="target-1",
    )
    with pytest.raises(TypeError, match="exact PermissionContext"):
        StandingPermissionUse(
            subject_id="agent-1",
            context=fake_context,  # type: ignore[arg-type]
            intent=intent,
            resource_id="resource-1",
        )


def test_standing_permission_use_requires_exact_action_intent() -> None:
    with pytest.raises(TypeError, match="exact ActionIntent"):
        StandingPermissionUse(
            subject_id="agent-1",
            context=PermissionContext("user-1", "project-1", "task-1"),
            intent=_IntentCarrier(),  # type: ignore[arg-type]
            resource_id="resource-1",
        )


def test_exact_nested_authority_carriers_remain_valid() -> None:
    context = PermissionContext("user-1", "project-1", "task-1")
    binding = StandingPermissionBinding(
        permission_id="perm-1",
        subject_id="agent-1",
        context=context,
        target="target-1",
        resource_id="resource-1",
        network_host=None,
    )
    intent = ActionIntent(
        action_id="call-1",
        tool_id="safe.read",
        risk=ToolRisk.READ_ONLY,
        target="target-1",
    )
    use = StandingPermissionUse(
        subject_id="agent-1",
        context=context,
        intent=intent,
        resource_id="resource-1",
    )

    assert binding.context is context
    assert use.context is context
    assert use.intent is intent


def test_standing_permission_rejects_equality_compatible_foreign_risk_carrier() -> None:
    forged = _forged_read_only_risk()

    assert forged == ToolRisk.READ_ONLY
    assert forged.value == ToolRisk.HIGH_IMPACT.value
    with pytest.raises(TypeError, match="exact ToolRisk"):
        _scope(risk_ceiling=forged)  # type: ignore[arg-type]


def test_forged_standing_risk_cannot_leave_durable_authority(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    permissions = StandingPermissionStore(store)
    permissions.initialize()

    with pytest.raises(TypeError, match="exact ToolRisk"):
        _grant_with_forged_risk(permissions)

    with store.connection() as conn:
        row_count = conn.execute(
            "SELECT COUNT(*) AS count FROM standing_permissions"
        ).fetchone()["count"]
    assert row_count == 0


@pytest.mark.parametrize(
    "risk",
    (
        ToolRisk.READ_ONLY,
        ToolRisk.LOCAL_WRITE,
        ToolRisk.EXTERNAL_SIDE_EFFECT,
    ),
)
def test_exact_non_high_impact_risk_ceiling_remains_valid(risk: ToolRisk) -> None:
    scope = _scope(risk_ceiling=risk)

    assert scope.risk_ceiling is risk


def test_exact_high_impact_ceiling_keeps_fresh_approval_requirement() -> None:
    with pytest.raises(ValueError, match="fresh explicit per-action approval"):
        _scope(risk_ceiling=ToolRisk.HIGH_IMPACT)
