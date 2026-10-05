from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.memory import MemoryScope, MemoryService


def _memory(tmp_path: Path) -> MemoryService:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    return MemoryService(store)


def test_namespace_read_only_expires_requested_scope_owner_and_namespace(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    expired = datetime(2035, 1, 1, tzinfo=UTC)
    after_expiry = expired + timedelta(days=1)
    before_expiry = expired - timedelta(days=1)

    for scope, owner_id, namespace, key in (
        (MemoryScope.WORKSPACE, "alpha", "notes", "expired"),
        (MemoryScope.WORKSPACE, "beta", "notes", "other_owner"),
        (MemoryScope.WORKSPACE, "alpha", "other", "other_namespace"),
        (MemoryScope.USER, "alpha", "notes", "other_scope"),
    ):
        memory.put(
            scope=scope,
            owner_id=owner_id,
            namespace=namespace,
            key=key,
            value={"key": key},
            user_approved=scope is MemoryScope.USER,
            expires_at=expired,
        )
    memory.put(
        scope=MemoryScope.WORKSPACE,
        owner_id="alpha",
        namespace="notes",
        key="live",
        value=True,
        expires_at=expired + timedelta(days=2),
    )

    records = memory.list_namespace(
        scope=MemoryScope.WORKSPACE,
        owner_id="alpha",
        namespace="notes",
        now=after_expiry,
    )
    assert [record.key for record in records] == ["live"]
    assert (
        memory.get(
            scope=MemoryScope.WORKSPACE,
            owner_id="alpha",
            namespace="notes",
            key="expired",
            now=before_expiry,
        )
        is None
    )

    for scope, owner_id, namespace, key in (
        (MemoryScope.WORKSPACE, "beta", "notes", "other_owner"),
        (MemoryScope.WORKSPACE, "alpha", "other", "other_namespace"),
        (MemoryScope.USER, "alpha", "notes", "other_scope"),
    ):
        assert (
            memory.get(
                scope=scope,
                owner_id=owner_id,
                namespace=namespace,
                key=key,
                now=before_expiry,
            )
            is not None
        )


def test_explicit_purge_still_deletes_expired_records_across_scopes(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    expired = datetime(2035, 1, 1, tzinfo=UTC)
    for scope in (MemoryScope.TASK, MemoryScope.WORKSPACE, MemoryScope.USER):
        memory.put(
            scope=scope,
            owner_id="owner",
            namespace="notes",
            key="expired",
            value=True,
            user_approved=scope is MemoryScope.USER,
            expires_at=expired,
        )
    assert memory.purge_expired(now=expired + timedelta(seconds=1)) == 3
    for scope in (MemoryScope.TASK, MemoryScope.WORKSPACE, MemoryScope.USER):
        assert memory.list_namespace(scope=scope, owner_id="owner", namespace="notes") == ()


def test_namespace_expiry_accepts_aware_offset_and_rejects_naive_time(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    expires = datetime(2035, 1, 1, tzinfo=UTC)
    memory.put(
        scope=MemoryScope.TASK,
        owner_id="task",
        namespace="scratch",
        key="temporary",
        value=1,
        expires_at=expires,
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        memory.list_namespace(
            scope=MemoryScope.TASK,
            owner_id="task",
            namespace="scratch",
            now=datetime(2036, 1, 1),  # noqa: DTZ001 - intentional naive-time rejection
        )
    assert len(
        memory.list_namespace(
            scope=MemoryScope.TASK,
            owner_id="task",
            namespace="scratch",
            now=datetime(2035, 1, 1, 1, tzinfo=timezone(timedelta(hours=2))),
        )
    ) == 1
    assert memory.list_namespace(
        scope=MemoryScope.TASK,
        owner_id="task",
        namespace="scratch",
        now=datetime(2035, 1, 1, 1, tzinfo=timezone(-timedelta(hours=2))),
    ) == ()

@pytest.mark.parametrize("approval", [1, "true", None, [], {"approved": True}])
def test_user_memory_requires_literal_boolean_approval(tmp_path: Path, approval: object) -> None:
    memory = _memory(tmp_path)
    with pytest.raises(ValueError, match="user_approved must be a boolean"):
        memory.put(
            scope=MemoryScope.USER,
            owner_id="owner",
            namespace="preferences",
            key="language",
            value="uk",
            user_approved=approval,
        )
    assert memory.get(
        scope=MemoryScope.USER,
        owner_id="owner",
        namespace="preferences",
        key="language",
    ) is None
    with pytest.raises(PermissionError, match="explicit approval"):
        memory.put(
            scope=MemoryScope.USER,
            owner_id="owner",
            namespace="preferences",
            key="language",
            value="uk",
            user_approved=False,
        )
    assert memory.put(
        scope=MemoryScope.USER,
        owner_id="owner",
        namespace="preferences",
        key="language",
        value="uk",
        user_approved=True,
    ).user_approved is True


@pytest.mark.parametrize(
    "invalid", [float("nan"), float("inf"), -float("inf"), {"nested": float("nan")}]
)
def test_invalid_json_numbers_cannot_overwrite_memory(tmp_path: Path, invalid: object) -> None:
    memory = _memory(tmp_path)
    params = {
        "scope": MemoryScope.TASK,
        "owner_id": "task",
        "namespace": "scratch",
        "key": "value",
    }
    memory.put(**params, value={"safe": True})
    with pytest.raises(ValueError, match="Out of range float values"):
        memory.put(**params, value=invalid)
    record = memory.get(**params)
    assert record is not None and record.value == {"safe": True}


def test_offset_expiry_in_namespace_preserves_live_and_removes_expired(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    memory = MemoryService(store)
    current = datetime(2038, 1, 1, tzinfo=UTC)
    for key in ("expired", "live"):
        memory.put(
            scope=MemoryScope.TASK,
            owner_id="owner",
            namespace="scratch",
            key=key,
            value=key,
            expires_at=current + timedelta(days=1),
        )
    with store.connection() as conn:
        conn.execute(
            "UPDATE memory_records SET expires_at = ? WHERE memory_key = 'expired'",
            ("2038-01-01T01:30:00+02:00",),
        )
        conn.execute(
            "UPDATE memory_records SET expires_at = ? WHERE memory_key = 'live'",
            ("2037-12-31T23:30:00-01:00",),
        )
    records = memory.list_namespace(
        scope=MemoryScope.TASK, owner_id="owner", namespace="scratch", now=current
    )
    assert [record.key for record in records] == ["live"]
    with store.connection() as conn:
        keys = [row[0] for row in conn.execute(
            "SELECT memory_key FROM memory_records ORDER BY memory_key"
        )]
    assert keys == ["live"]


def test_global_purge_respects_non_utc_expiry_offsets(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    memory = MemoryService(store)
    current = datetime(2038, 1, 1, tzinfo=UTC)
    for key in ("expired", "live"):
        memory.put(
            scope=MemoryScope.TASK,
            owner_id="owner",
            namespace="scratch",
            key=key,
            value=key,
            expires_at=current + timedelta(days=1),
        )
    with store.connection() as conn:
        conn.execute(
            "UPDATE memory_records SET expires_at = ? WHERE memory_key = 'expired'",
            ("2038-01-01T01:30:00+02:00",),
        )
        conn.execute(
            "UPDATE memory_records SET expires_at = ? WHERE memory_key = 'live'",
            ("2037-12-31T23:30:00-01:00",),
        )
    assert memory.purge_expired(now=current) == 1
    with store.connection() as conn:
        keys = [row[0] for row in conn.execute(
            "SELECT memory_key FROM memory_records ORDER BY memory_key"
        )]
    assert keys == ["live"]


@pytest.mark.parametrize("operation", ["list", "purge"])
def test_invalid_expiry_rolls_back_cleanup(tmp_path: Path, operation: str) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    memory = MemoryService(store)
    current = datetime(2038, 1, 1, tzinfo=UTC)
    for key in ("a_expired", "z_invalid"):
        memory.put(
            scope=MemoryScope.TASK,
            owner_id="owner",
            namespace="scratch",
            key=key,
            value=key,
            expires_at=current + timedelta(days=1),
        )
    with store.connection() as conn:
        conn.execute(
            "UPDATE memory_records SET expires_at = ? WHERE memory_key = 'a_expired'",
            ("2038-01-01T01:30:00+02:00",),
        )
        conn.execute(
            "UPDATE memory_records SET expires_at = ? WHERE memory_key = 'z_invalid'",
            ("not-a-datetime",),
        )
    with pytest.raises(ValueError):
        if operation == "list":
            memory.list_namespace(
                scope=MemoryScope.TASK, owner_id="owner", namespace="scratch", now=current
            )
        else:
            memory.purge_expired(now=current)
    with store.connection() as conn:
        keys = [row[0] for row in conn.execute(
            "SELECT memory_key FROM memory_records ORDER BY memory_key"
        )]
    assert keys == ["a_expired", "z_invalid"]
