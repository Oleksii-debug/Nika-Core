from __future__ import annotations

import asyncio
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
    StandingPermissionPolicy,
    StandingPermissionScope,
    StandingPermissionStore,
    StandingPermissionUse,
)
from nika_core.tools import ToolCall, ToolRisk, ToolSpec


class _ForgedRisk(str):
    @property
    def value(self) -> str:
        return ToolRisk.HIGH_IMPACT.value


class _MutableContext:
    def __init__(self) -> None:
        self.user_id = "user-1"
        self.project_id = "project-1"
        self.task_id = "task-1"


class _HashRebindingText(str):
    def __new__(cls, visible: str, hashed_as: str):
        value = super().__new__(cls, visible)
        value._hashed_as = hashed_as
        return value

    def encode(self, encoding="utf-8", errors="strict"):
        return self._hashed_as.encode(encoding, errors)


class _TupleCarrier(tuple):
    pass


class _DateTimeCarrier(datetime):
    pass


class _IntentCarrier:
    tool_id = "safe.read"
    target = "target-1"
    network_host = None
    risk = ToolRisk.READ_ONLY


class _ProxyCarrier:
    def __init__(self, value) -> None:
        self._value = value

    def __getattr__(self, name):
        return getattr(self._value, name)


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


def test_grant_rejects_duck_typed_scope_before_durable_write(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "duck-grant.db")
    permissions = StandingPermissionStore(store)
    permissions.initialize()
    scope = _scope(risk_ceiling=ToolRisk.READ_ONLY)

    with pytest.raises(TypeError, match="exact StandingPermissionScope"):
        permissions.grant(
            permission_id="duck-scope",
            scope=_ProxyCarrier(scope),  # type: ignore[arg-type]
        )

    with store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS count FROM standing_permissions"
        ).fetchone()["count"] == 0


def test_delegate_rejects_duck_typed_scope_before_child_write(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "duck-delegate.db")
    permissions = StandingPermissionStore(store)
    permissions.initialize()
    parent = _scope(risk_ceiling=ToolRisk.READ_ONLY)
    permissions.grant(permission_id="perm-parent", scope=parent)

    child = StandingPermissionScope(
        subject_id="agent-child",
        context=parent.context,
        action_class=parent.action_class,
        targets=parent.targets,
        sites=parent.sites,
        resources=parent.resources,
        risk_ceiling=parent.risk_ceiling,
        granted_at=parent.granted_at + timedelta(seconds=1),
        expires_at=parent.expires_at,
    )
    with pytest.raises(TypeError, match="exact StandingPermissionScope"):
        permissions.delegate(
            parent_permission_id="perm-parent",
            permission_id="perm-child",
            scope=_ProxyCarrier(child),  # type: ignore[arg-type]
            delegated_by_subject_id="agent-1",
        )
    assert permissions.get("perm-child") is None


def test_authorize_rejects_duck_typed_use_carrier(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "duck-use.db")
    permissions = StandingPermissionStore(store)
    permissions.initialize()
    scope = _scope(risk_ceiling=ToolRisk.READ_ONLY)
    permissions.grant(permission_id="perm-use", scope=scope)
    use = StandingPermissionUse(
        subject_id="agent-1",
        context=scope.context,
        intent=ActionIntent(
            action_id="call-1",
            tool_id="safe.read",
            risk=ToolRisk.READ_ONLY,
            target="target-1",
        ),
        resource_id="resource-1",
    )

    with pytest.raises(TypeError, match="exact StandingPermissionUse"):
        permissions.authorize(
            "perm-use",
            _ProxyCarrier(use),  # type: ignore[arg-type]
            now=scope.granted_at + timedelta(seconds=1),
        )


def test_policy_rejects_duck_typed_binding_carrier(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "duck-binding.db")
    permissions = StandingPermissionStore(store)
    permissions.initialize()
    binding = StandingPermissionBinding(
        permission_id="perm-1",
        subject_id="agent-1",
        context=PermissionContext("user-1", "project-1", "task-1"),
        target="target-1",
        resource_id="resource-1",
        network_host=None,
    )

    with pytest.raises(TypeError, match="exact StandingPermissionBinding"):
        StandingPermissionPolicy(
            permissions,
            _ProxyCarrier(binding),  # type: ignore[arg-type]
        )


def test_policy_snapshots_binding_authority_before_retained_object_mutation(
    tmp_path,
) -> None:
    store = SQLiteStore(tmp_path / "binding-snapshot.db")
    permissions = StandingPermissionStore(store)
    permissions.initialize()
    now = datetime(2026, 9, 27, tzinfo=UTC)

    def scope(suffix: str) -> StandingPermissionScope:
        return StandingPermissionScope(
            subject_id=f"agent-{suffix}",
            context=PermissionContext(
                f"user-{suffix}",
                f"project-{suffix}",
                f"task-{suffix}",
            ),
            action_class="safe.read",
            targets=(f"target-{suffix}",),
            sites=(),
            resources=(f"resource-{suffix}",),
            risk_ceiling=ToolRisk.EXTERNAL_SIDE_EFFECT,
            granted_at=now,
            expires_at=now + timedelta(minutes=5),
        )

    scope_a = scope("a")
    scope_b = scope("b")
    permissions.grant(permission_id="perm-a", scope=scope_a)
    permissions.grant(permission_id="perm-b", scope=scope_b)
    binding = StandingPermissionBinding(
        permission_id="perm-a",
        subject_id="agent-a",
        context=scope_a.context,
        target="target-a",
        resource_id="resource-a",
        network_host=None,
    )
    policy = StandingPermissionPolicy(
        permissions,
        binding,
        clock=lambda: now + timedelta(seconds=1),
    )

    object.__setattr__(binding, "permission_id", "perm-b")
    object.__setattr__(binding, "subject_id", "agent-b")
    object.__setattr__(binding, "context", scope_b.context)
    object.__setattr__(binding, "target", "target-b")
    object.__setattr__(binding, "resource_id", "resource-b")

    spec = ToolSpec(
        tool_id="safe.read",
        description="read with standing authority",
        risk=ToolRisk.EXTERNAL_SIDE_EFFECT,
    )
    redirected = ToolCall(
        call_id="call-b",
        tool_id="safe.read",
        arguments={},
        task_id="task-b",
    )
    with pytest.raises(PermissionError, match="task is outside standing permission context"):
        asyncio.run(policy(spec, redirected))

    original = ToolCall(
        call_id="call-a",
        tool_id="safe.read",
        arguments={},
        task_id="task-a",
    )
    authorization = asyncio.run(policy(spec, original))

    assert authorization.task_id == "task-a"
    assert authorization.tool_id == "safe.read"
    assert authorization.risk is ToolRisk.EXTERNAL_SIDE_EFFECT


def test_policy_revalidates_exact_binding_fields_before_snapshot(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "binding-revalidation.db")
    permissions = StandingPermissionStore(store)
    permissions.initialize()
    binding = StandingPermissionBinding(
        permission_id="perm-a",
        subject_id="agent-a",
        context=PermissionContext("user-a", "project-a", "task-a"),
        target="target-a",
        resource_id="resource-a",
        network_host=None,
    )
    object.__setattr__(
        binding,
        "subject_id",
        _HashRebindingText("agent-visible", "agent-authorized"),
    )

    with pytest.raises(ValueError, match="canonical identity"):
        StandingPermissionPolicy(permissions, binding)


def test_hash_rebinding_text_cannot_enter_permission_context() -> None:
    forged_user = _HashRebindingText("user-visible", "user-authorized")

    assert str(forged_user) == "user-visible"
    assert forged_user.encode() == b"user-authorized"
    with pytest.raises(ValueError, match="canonical identity"):
        PermissionContext(
            user_id=forged_user,  # type: ignore[arg-type]
            project_id="project-1",
            task_id="task-1",
        )


def test_scope_vectors_require_exact_tuple_container() -> None:
    now = datetime(2026, 9, 27, tzinfo=UTC)
    with pytest.raises(ValueError, match="explicit non-empty tuple"):
        StandingPermissionScope(
            subject_id="agent-1",
            context=PermissionContext("user-1", "project-1", "task-1"),
            action_class="safe.read",
            targets=_TupleCarrier(("target-1",)),  # type: ignore[arg-type]
            sites=(),
            resources=("resource-1",),
            risk_ceiling=ToolRisk.READ_ONLY,
            granted_at=now,
            expires_at=now + timedelta(minutes=5),
        )


def test_scope_times_require_exact_datetime_carrier() -> None:
    forged_time = _DateTimeCarrier(2026, 9, 27, tzinfo=UTC)
    with pytest.raises(ValueError, match="timezone-aware"):
        StandingPermissionScope(
            subject_id="agent-1",
            context=PermissionContext("user-1", "project-1", "task-1"),
            action_class="safe.read",
            targets=("target-1",),
            sites=(),
            resources=("resource-1",),
            risk_ceiling=ToolRisk.READ_ONLY,
            granted_at=forged_time,
            expires_at=datetime(2026, 9, 27, 0, 5, tzinfo=UTC),
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


def test_text_migration_schema_is_rejected_before_version_coercion(tmp_path) -> None:
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
    with pytest.raises(StandingPermissionIntegrityError, match="schema shape"):
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


def test_parent_authority_foreign_key_is_part_of_durable_schema(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "missing-parent-fk.db")
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
            "scope_json TEXT NOT NULL, "
            "scope_fingerprint TEXT NOT NULL, "
            "revoked_at TEXT)"
        )

    permissions = StandingPermissionStore(store)
    with pytest.raises(StandingPermissionIntegrityError, match="foreign key"):
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
