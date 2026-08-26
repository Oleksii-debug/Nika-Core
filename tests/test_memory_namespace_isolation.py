from datetime import UTC, datetime, timedelta
from pathlib import Path

from nika_core.data.sqlite import SQLiteStore
from nika_core.memory.contracts import MemoryScope
from nika_core.memory.service import MemoryService


def test_namespace_read_does_not_purge_unrelated_expired_memory(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    memory = MemoryService(store)
    baseline = datetime.now(UTC)

    memory.put(
        scope=MemoryScope.WORKSPACE,
        owner_id="workspace-a",
        namespace="notes",
        key="live",
        value={"text": "keep"},
        expires_at=baseline + timedelta(days=60),
    )
    memory.put(
        scope=MemoryScope.AGENT,
        owner_id="agent-b",
        namespace="private",
        key="unrelated",
        value={"text": "separate"},
        expires_at=baseline + timedelta(days=30),
    )

    observed_at = baseline + timedelta(days=45)
    records = memory.list_namespace(
        scope=MemoryScope.WORKSPACE,
        owner_id="workspace-a",
        namespace="notes",
        now=observed_at,
    )

    assert [record.key for record in records] == ["live"]
    with store.connection() as conn:
        unrelated = conn.execute(
            "SELECT 1 FROM memory_records WHERE scope = ? AND owner_id = ? "
            "AND namespace = ? AND memory_key = ?",
            (MemoryScope.AGENT.value, "agent-b", "private", "unrelated"),
        ).fetchone()
    assert unrelated is not None

    assert memory.purge_expired(now=observed_at) == 1
    with store.connection() as conn:
        remaining = conn.execute(
            "SELECT scope, owner_id, namespace, memory_key FROM memory_records"
        ).fetchall()
    assert [tuple(row) for row in remaining] == [
        (MemoryScope.WORKSPACE.value, "workspace-a", "notes", "live")
    ]


def test_namespace_read_omits_own_expired_records_without_deleting_them(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    memory = MemoryService(store)
    baseline = datetime.now(UTC)

    memory.put(
        scope=MemoryScope.WORKSPACE,
        owner_id="workspace-a",
        namespace="notes",
        key="expired",
        value={"text": "old"},
        expires_at=baseline + timedelta(days=30),
    )
    memory.put(
        scope=MemoryScope.WORKSPACE,
        owner_id="workspace-a",
        namespace="notes",
        key="live",
        value={"text": "new"},
        expires_at=baseline + timedelta(days=60),
    )

    observed_at = baseline + timedelta(days=45)
    records = memory.list_namespace(
        scope=MemoryScope.WORKSPACE,
        owner_id="workspace-a",
        namespace="notes",
        now=observed_at,
    )

    assert [record.key for record in records] == ["live"]
    with store.connection() as conn:
        keys = [
            row["memory_key"]
            for row in conn.execute(
                "SELECT memory_key FROM memory_records "
                "WHERE scope = ? AND owner_id = ? AND namespace = ? ORDER BY memory_key",
                (MemoryScope.WORKSPACE.value, "workspace-a", "notes"),
            ).fetchall()
        ]
    assert keys == ["expired", "live"]
    assert memory.purge_expired(now=observed_at) == 1
