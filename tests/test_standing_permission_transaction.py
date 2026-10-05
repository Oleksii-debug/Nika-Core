from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.security.standing_permission import (
    PermissionContext,
    StandingPermissionScope,
    StandingPermissionStore,
)
from nika_core.tools import ToolRisk


NOW = datetime(2026, 10, 5, 4, 55, tzinfo=UTC)


def _authority(tmp_path: Path) -> tuple[SQLiteStore, AuditLog, StandingPermissionStore]:
    store = SQLiteStore(tmp_path / "standing-permission-transaction.sqlite3")
    store.initialize()
    audit = AuditLog(store)
    permissions = StandingPermissionStore(store, audit_log=audit)
    permissions.initialize()
    return store, audit, permissions


def _scope() -> StandingPermissionScope:
    return StandingPermissionScope(
        subject_id="nika.packaged.model",
        context=PermissionContext(
            user_id="nika.local.user",
            project_id="default",
            task_id="task-atomic-grant",
        ),
        action_class="model.cloud.complete",
        targets=("configured-api",),
        sites=("api.example.test",),
        resources=("api-model",),
        risk_ceiling=ToolRisk.EXTERNAL_SIDE_EFFECT,
        granted_at=NOW,
        expires_at=NOW + timedelta(hours=24),
    )


def test_grant_transaction_rolls_back_grant_and_audit_on_dependent_failure(
    tmp_path: Path,
) -> None:
    store, audit, permissions = _authority(tmp_path)
    permission_id = "model-cloud:atomic-rollback"

    with pytest.raises(RuntimeError, match="dependent binding failed"):
        with permissions.grant_transaction(
            permission_id=permission_id,
            scope=_scope(),
        ) as (conn, granted):
            assert granted.permission_id == permission_id
            row = conn.execute(
                "SELECT permission_id FROM standing_permissions WHERE permission_id = ?",
                (permission_id,),
            ).fetchone()
            assert row["permission_id"] == permission_id
            raise RuntimeError("dependent binding failed")

    assert permissions.get(permission_id) is None
    assert audit.list_for(
        entity_type="standing_permission",
        entity_id=permission_id,
    ) == ()
    with store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM standing_permissions WHERE permission_id = ?",
            (permission_id,),
        ).fetchone()[0] == 0


def test_grant_transaction_keeps_commit_control_inside_store(
    tmp_path: Path,
) -> None:
    store, audit, permissions = _authority(tmp_path)
    permission_id = "model-cloud:no-caller-commit"
    with store.connection() as conn:
        conn.execute(
            "CREATE TABLE dependent_atomicity_probe(value TEXT NOT NULL)"
        )

    with pytest.raises(RuntimeError, match="dependent write rejected"):
        with permissions.grant_transaction(
            permission_id=permission_id,
            scope=_scope(),
        ) as (transaction, _granted):
            assert not hasattr(transaction, "commit")
            assert not hasattr(transaction, "rollback")
            assert not hasattr(transaction, "close")
            transaction.execute(
                "INSERT INTO dependent_atomicity_probe(value) VALUES (?)",
                ("uncommitted",),
            )
            with pytest.raises(ValueError, match="only permits"):
                transaction.execute("COMMIT")
            raise RuntimeError("dependent write rejected")

    assert permissions.get(permission_id) is None
    assert audit.list_for(
        entity_type="standing_permission",
        entity_id=permission_id,
    ) == ()
    with store.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM dependent_atomicity_probe"
        ).fetchone()[0]
    assert count == 0


@pytest.mark.parametrize(
    "unsafe_sql",
    (
        "BEGIN",
        "ROLLBACK",
        "END",
        "SAVEPOINT nested",
        "RELEASE nested",
        "CREATE TABLE escaped(value TEXT)",
        "PRAGMA foreign_keys = OFF",
        "ATTACH DATABASE ':memory:' AS escaped",
    ),
)
def test_grant_transaction_rejects_transaction_control_and_non_dml_sql(
    tmp_path: Path,
    unsafe_sql: str,
) -> None:
    _store, audit, permissions = _authority(tmp_path)
    permission_id = "model-cloud:sql-boundary"

    with pytest.raises(ValueError, match="only permits"):
        with permissions.grant_transaction(
            permission_id=permission_id,
            scope=_scope(),
        ) as (transaction, _granted):
            transaction.execute(unsafe_sql)

    assert permissions.get(permission_id) is None
    assert audit.list_for(
        entity_type="standing_permission",
        entity_id=permission_id,
    ) == ()


class _HostileSQL(str):
    def lstrip(self, *args: object, **kwargs: object) -> str:
        del args, kwargs
        raise AssertionError("hostile SQL methods must not run")


def test_grant_transaction_rejects_nonexact_sql_before_string_methods(
    tmp_path: Path,
) -> None:
    _store, audit, permissions = _authority(tmp_path)
    permission_id = "model-cloud:hostile-sql"

    with pytest.raises(TypeError, match="exact text"):
        with permissions.grant_transaction(
            permission_id=permission_id,
            scope=_scope(),
        ) as (transaction, _granted):
            transaction.execute(_HostileSQL("COMMIT"))

    assert permissions.get(permission_id) is None
    assert audit.list_for(
        entity_type="standing_permission",
        entity_id=permission_id,
    ) == ()


def test_grant_transaction_abrupt_exit_leaves_no_grant_or_audit(
    tmp_path: Path,
) -> None:
    _store, audit, permissions = _authority(tmp_path)
    permission_id = "model-cloud:atomic-system-exit"

    with pytest.raises(SystemExit):
        with permissions.grant_transaction(
            permission_id=permission_id,
            scope=_scope(),
        ):
            raise SystemExit("simulated process exit")

    assert permissions.get(permission_id) is None
    assert audit.list_for(
        entity_type="standing_permission",
        entity_id=permission_id,
    ) == ()


def test_grant_transaction_commits_grant_and_dependent_write_together(
    tmp_path: Path,
) -> None:
    store, audit, permissions = _authority(tmp_path)
    permission_id = "model-cloud:atomic-commit"

    with store.connection() as conn:
        conn.execute(
            "CREATE TABLE dependent_binding ("
            "task_id TEXT PRIMARY KEY, permission_id TEXT NOT NULL "
            "REFERENCES standing_permissions(permission_id))"
        )

    with permissions.grant_transaction(
        permission_id=permission_id,
        scope=_scope(),
    ) as (conn, granted):
        conn.execute(
            "INSERT INTO dependent_binding(task_id, permission_id) VALUES (?, ?)",
            ("task-atomic-grant", granted.permission_id),
        )

    assert permissions.get(permission_id) is not None
    assert [event.event_type for event in audit.list_for(
        entity_type="standing_permission",
        entity_id=permission_id,
    )] == ["standing_permission.granted"]
    with store.connection() as conn:
        row = conn.execute(
            "SELECT task_id, permission_id FROM dependent_binding"
        ).fetchone()
    assert row["task_id"] == "task-atomic-grant"
    assert row["permission_id"] == permission_id
