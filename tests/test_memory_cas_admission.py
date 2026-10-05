from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.memory import MemoryConflictError, MemoryScope, MemoryService


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    return store


def _identity() -> dict[str, object]:
    return {
        "scope": MemoryScope.WORKSPACE,
        "owner_id": "research",
        "namespace": "policy",
        "key": "ranking",
    }


def test_conditional_put_minimizes_secret_before_durable_write(tmp_path: Path) -> None:
    store = _store(tmp_path)
    memory = MemoryService(store)
    secret = "sk-cas-secret-123"

    record = memory.compare_and_put(
        **_identity(),
        value={"api_key": secret, "status": "ok"},
        expected_updated_at=None,
    )

    assert record.value == {"api_key": "[REDACTED]", "status": "ok"}
    with store.connection() as conn:
        row = conn.execute(
            "SELECT value_json FROM memory_records WHERE scope = ? AND owner_id = ? "
            "AND namespace = ? AND memory_key = ?",
            ("workspace", "research", "policy", "ranking"),
        ).fetchone()
    assert row is not None
    assert secret not in row["value_json"]
    assert '"api_key":"[REDACTED]"' in row["value_json"]


def test_conditional_create_does_not_cleanup_corrupt_expired_record(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    memory = MemoryService(store)
    memory.put(
        **_identity(),
        value={"state": "original"},
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    expired_at = datetime.now(UTC) - timedelta(seconds=1)
    with store.connection() as conn:
        conn.execute(
            "UPDATE memory_records SET expires_at = ?, value_json = 'NaN' "
            "WHERE scope = ? AND owner_id = ? AND namespace = ? AND memory_key = ?",
            (
                expired_at.isoformat(),
                "workspace",
                "research",
                "policy",
                "ranking",
            ),
        )

    with pytest.raises(ValueError, match="invalid stored memory JSON constant"):
        memory.compare_and_put(
            **_identity(),
            value={"state": "replacement"},
            expected_updated_at=None,
        )

    with store.connection() as conn:
        row = conn.execute(
            "SELECT value_json, expires_at FROM memory_records "
            "WHERE scope = ? AND owner_id = ? AND namespace = ? AND memory_key = ?",
            ("workspace", "research", "policy", "ranking"),
        ).fetchone()
    assert row is not None
    assert row["value_json"] == "NaN"
    assert row["expires_at"] == expired_at.isoformat()


def test_compare_and_delete_validates_full_record_before_mutation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    audit = AuditLog(store)
    memory = MemoryService(store, audit)
    original = memory.put(**_identity(), value={"state": "original"})
    with store.connection() as conn:
        conn.execute(
            "UPDATE memory_records SET value_json = 'NaN' "
            "WHERE scope = ? AND owner_id = ? AND namespace = ? AND memory_key = ?",
            ("workspace", "research", "policy", "ranking"),
        )

    with pytest.raises(ValueError, match="invalid stored memory JSON constant"):
        memory.compare_and_delete(
            **_identity(),
            expected_updated_at=original.updated_at,
        )

    with store.connection() as conn:
        row = conn.execute(
            "SELECT value_json FROM memory_records WHERE scope = ? AND owner_id = ? "
            "AND namespace = ? AND memory_key = ?",
            ("workspace", "research", "policy", "ranking"),
        ).fetchone()
    assert row is not None and row["value_json"] == "NaN"
    events = audit.list_for(
        entity_type="memory",
        entity_id="workspace:research:policy:ranking",
    )
    assert [event.event_type for event in events] == ["memory.upserted"]


@pytest.mark.parametrize("user_approved", [1, "yes", object()])
def test_conditional_user_memory_requires_literal_boolean_approval(
    tmp_path: Path,
    user_approved: object,
) -> None:
    store = _store(tmp_path)
    memory = MemoryService(store)

    with pytest.raises(ValueError, match="user_approved must be a boolean"):
        memory.compare_and_put(
            scope=MemoryScope.USER,
            owner_id="local-user",
            namespace="preferences",
            key="language",
            value="uk",
            expected_updated_at=None,
            user_approved=user_approved,  # type: ignore[arg-type]
        )

    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0] == 0


@pytest.mark.parametrize(
    "scope",
    [
        "workspace",
        object(),
    ],
)
def test_conditional_mutations_require_exact_memory_scope(
    tmp_path: Path,
    scope: object,
) -> None:
    store = _store(tmp_path)
    memory = MemoryService(store)

    with pytest.raises(ValueError, match="scope must be a MemoryScope"):
        memory.compare_and_put(
            scope=scope,  # type: ignore[arg-type]
            owner_id="research",
            namespace="policy",
            key="ranking",
            value={"state": "unsafe"},
            expected_updated_at=None,
        )

    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0] == 0


@pytest.mark.parametrize("expected", [1, True, "2038-01-01T00:00:00+00:00"])
def test_conditional_put_rejects_non_datetime_revision_before_sql(
    tmp_path: Path,
    expected: object,
) -> None:
    store = _store(tmp_path)
    memory = MemoryService(store)

    with pytest.raises(ValueError, match="expected_updated_at must be a datetime or None"):
        memory.compare_and_put(
            **_identity(),
            value={"state": "unsafe"},
            expected_updated_at=expected,  # type: ignore[arg-type]
        )

    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0] == 0


def test_stale_conditional_write_preserves_newer_minimized_winner(tmp_path: Path) -> None:
    store = _store(tmp_path)
    memory = MemoryService(store)
    original = memory.compare_and_put(
        **_identity(),
        value={"token": "first-secret"},
        expected_updated_at=None,
    )
    winner = memory.compare_and_put(
        **_identity(),
        value={"token": "winner-secret"},
        expected_updated_at=original.updated_at,
    )

    with pytest.raises(MemoryConflictError, match="revision changed"):
        memory.compare_and_put(
            **_identity(),
            value={"token": "stale-secret"},
            expected_updated_at=original.updated_at,
        )

    durable = memory.get(**_identity())
    assert durable == winner
    assert durable is not None and durable.value == {"token": "[REDACTED]"}
    with store.connection() as conn:
        row = conn.execute(
            "SELECT value_json FROM memory_records WHERE scope = ? AND owner_id = ? "
            "AND namespace = ? AND memory_key = ?",
            ("workspace", "research", "policy", "ranking"),
        ).fetchone()
    assert row is not None
    assert "winner-secret" not in row["value_json"]
    assert "stale-secret" not in row["value_json"]
