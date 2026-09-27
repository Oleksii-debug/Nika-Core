from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.security.policy import ActionIntent
from nika_core.security.standing_permission import (
    PermissionContext,
    StandingPermissionBinding,
    StandingPermissionIntegrityError,
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


def _scope(
    *,
    risk_ceiling: ToolRisk,
    action_class: str = "safe.read",
) -> StandingPermissionScope:
    now = datetime(2026, 9, 27, tzinfo=UTC)
    return StandingPermissionScope(
        subject_id="agent-1",
        context=PermissionContext(
            user_id="user-1",
            project_id="project-1",
            task_id="task-1",
        ),
        action_class=action_class,
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


def _canonical_permission(tmp_path, *, action_class: str = "safe.read"):
    store = SQLiteStore(tmp_path / "durable-authority.db")
    permissions = StandingPermissionStore(store)
    permissions.initialize()
    permission = permissions.grant(
        permission_id="perm-durable",
        scope=_scope(
            risk_ceiling=ToolRisk.READ_ONLY,
            action_class=action_class,
        ),
    )
    return store, permission


def test_scope_json_blob_storage_fails_closed_on_restart(tmp_path) -> None:
    store, _permission = _canonical_permission(tmp_path)
    with store.connection() as conn:
        row = conn.execute(
            "SELECT scope_json FROM standing_permissions WHERE permission_id = ?",
            ("perm-durable",),
        ).fetchone()
        conn.execute(
            "UPDATE standing_permissions SET scope_json = ? WHERE permission_id = ?",
            (sqlite3.Binary(row["scope_json"].encode("utf-8")), "perm-durable"),
        )

    restarted = StandingPermissionStore(store)
    restarted.initialize()
    with pytest.raises(StandingPermissionIntegrityError, match="storage type"):
        restarted.get("perm-durable")


def test_unknown_durable_scope_field_is_rejected(tmp_path) -> None:
    store, _permission = _canonical_permission(tmp_path)
    with store.connection() as conn:
        row = conn.execute(
            "SELECT scope_json FROM standing_permissions WHERE permission_id = ?",
            ("perm-durable",),
        ).fetchone()
        payload = json.loads(row["scope_json"])
        payload["ignored_authority"] = "must-not-normalize"
        conn.execute(
            "UPDATE standing_permissions SET scope_json = ? WHERE permission_id = ?",
            (
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "perm-durable",
            ),
        )

    restarted = StandingPermissionStore(store)
    restarted.initialize()
    with pytest.raises(
        StandingPermissionIntegrityError,
        match="unexpected durable fields",
    ):
        restarted.get("perm-durable")


def test_durable_scope_scalar_type_is_not_string_coerced(tmp_path) -> None:
    # "7" is a valid canonical action class. Replacing the JSON string with numeric
    # 7 used to round-trip through str(7) and preserve the stored fingerprint.
    store, _permission = _canonical_permission(tmp_path, action_class="7")
    with store.connection() as conn:
        row = conn.execute(
            "SELECT scope_json FROM standing_permissions WHERE permission_id = ?",
            ("perm-durable",),
        ).fetchone()
        payload = json.loads(row["scope_json"])
        payload["action_class"] = 7
        conn.execute(
            "UPDATE standing_permissions SET scope_json = ? WHERE permission_id = ?",
            (
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "perm-durable",
            ),
        )

    restarted = StandingPermissionStore(store)
    restarted.initialize()
    with pytest.raises(StandingPermissionIntegrityError, match="durable text"):
        restarted.get("perm-durable")


def test_text_migration_version_is_not_integer_coerced(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "bad-migration.db")
    with store.connection() as conn:
        conn.execute(
            "CREATE TABLE standing_permission_schema_migrations ("
            "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO standing_permission_schema_migrations(version, applied_at) "
            "VALUES (?, ?)",
            ("1", datetime(2026, 9, 27, tzinfo=UTC).isoformat()),
        )

    permissions = StandingPermissionStore(store)
    with pytest.raises(StandingPermissionIntegrityError, match="schema version.*storage type"):
        permissions.initialize()


def test_standing_permission_schema_shape_is_validated(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "bad-shape.db")
    with store.connection() as conn:
        conn.execute(
            "CREATE TABLE standing_permission_schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO standing_permission_schema_migrations(version, applied_at) "
            "VALUES (1, ?)",
            (datetime(2026, 9, 27, tzinfo=UTC).isoformat(),),
        )
        conn.execute(
            "CREATE TABLE standing_permissions ("
            "permission_id TEXT PRIMARY KEY, "
            "parent_permission_id TEXT, "
            "scope_json BLOB NOT NULL, "
            "scope_fingerprint TEXT NOT NULL, "
            "revoked_at TEXT)"
        )

    permissions = StandingPermissionStore(store)
    with pytest.raises(StandingPermissionIntegrityError, match="schema shape"):
        permissions.initialize()


def test_canonical_durable_authority_survives_restart(tmp_path) -> None:
    store, original = _canonical_permission(tmp_path)

    restarted = StandingPermissionStore(store)
    restarted.initialize()
    restored = restarted.get("perm-durable")

    assert restored is not None
    assert restored.permission_id == original.permission_id
    assert restored.scope_fingerprint == original.scope_fingerprint
    assert restored.parent_permission_id is None


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
