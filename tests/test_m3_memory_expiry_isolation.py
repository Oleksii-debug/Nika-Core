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
            now=datetime(2036, 1, 1),
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
