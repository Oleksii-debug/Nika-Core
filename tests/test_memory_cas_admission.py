from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event

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


@pytest.mark.parametrize(
    ("corrupt_json", "match"),
    [
        ("[" * 65 + "0" + "]" * 65, "depth limit"),
        ('{"value":' + "9" * 1235 + "}", "digit limit"),
        ('{"value":' + str(1 << 4096) + "}", "bit limit"),
    ],
)
def test_persisted_memory_resource_carriers_fail_closed_before_use(
    tmp_path: Path,
    corrupt_json: str,
    match: str,
) -> None:
    store = _store(tmp_path)
    memory = MemoryService(store)
    memory.put(**_identity(), value={"state": "original"})

    with store.connection() as conn:
        cursor = conn.execute(
            "UPDATE memory_records SET value_json = ? "
            "WHERE scope = ? AND owner_id = ? AND namespace = ? AND memory_key = ?",
            (
                corrupt_json,
                "workspace",
                "research",
                "policy",
                "ranking",
            ),
        )
        assert cursor.rowcount == 1

    with pytest.raises(ValueError, match=match):
        memory.get(**_identity())


def test_persisted_memory_integer_accepts_exact_bit_boundary(tmp_path: Path) -> None:
    store = _store(tmp_path)
    memory = MemoryService(store)
    memory.put(**_identity(), value={"state": "original"})
    boundary = 1 << 4095

    with store.connection() as conn:
        cursor = conn.execute(
            "UPDATE memory_records SET value_json = ? "
            "WHERE scope = ? AND owner_id = ? AND namespace = ? AND memory_key = ?",
            (
                '{"value":' + str(boundary) + "}",
                "workspace",
                "research",
                "policy",
                "ranking",
            ),
        )
        assert cursor.rowcount == 1

    restored = memory.get(**_identity())
    assert restored is not None
    assert restored.value == {"value": boundary}


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


class _BehavioralDateTime(datetime):
    def astimezone(self, tz=None):  # type: ignore[override]
        raise AssertionError("datetime subclass behavior must not execute")


@pytest.mark.parametrize("operation", ["put", "get", "list_namespace", "purge_expired"])
def test_public_temporal_boundaries_reject_datetime_subclasses_before_behavior(
    tmp_path: Path,
    operation: str,
) -> None:
    store = _store(tmp_path)
    memory = MemoryService(store)
    hostile = _BehavioralDateTime(2026, 10, 5, 8, 0, tzinfo=UTC)

    with pytest.raises(ValueError, match="datetime must be an exact datetime"):
        if operation == "put":
            memory.put(**_identity(), value={"state": "unsafe"}, expires_at=hostile)
        elif operation == "get":
            memory.get(**_identity(), now=hostile)
        elif operation == "list_namespace":
            memory.list_namespace(
                scope=MemoryScope.WORKSPACE,
                owner_id="research",
                namespace="policy",
                now=hostile,
            )
        else:
            memory.purge_expired(now=hostile)

    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0] == 0


@pytest.mark.parametrize("invalid_now", [False, 0, "", object()])
@pytest.mark.parametrize("operation", ["get", "list_namespace", "purge_expired"])
def test_explicit_invalid_now_is_not_silently_treated_as_omitted(
    tmp_path: Path,
    operation: str,
    invalid_now: object,
) -> None:
    memory = MemoryService(_store(tmp_path))

    with pytest.raises(ValueError, match="datetime must be an exact datetime"):
        if operation == "get":
            memory.get(**_identity(), now=invalid_now)  # type: ignore[arg-type]
        elif operation == "list_namespace":
            memory.list_namespace(
                scope=MemoryScope.WORKSPACE,
                owner_id="research",
                namespace="policy",
                now=invalid_now,  # type: ignore[arg-type]
            )
        else:
            memory.purge_expired(now=invalid_now)  # type: ignore[arg-type]


@pytest.mark.parametrize("invalid_expiry", [False, 0, "", object()])
def test_explicit_invalid_expiry_fails_before_memory_mutation(
    tmp_path: Path,
    invalid_expiry: object,
) -> None:
    store = _store(tmp_path)
    memory = MemoryService(store)

    with pytest.raises(ValueError, match="datetime must be an exact datetime"):
        memory.put(
            **_identity(),
            value={"state": "unsafe"},
            expires_at=invalid_expiry,  # type: ignore[arg-type]
        )

    with store.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM memory_records").fetchone()[0] == 0


def test_expired_write_commits_cleanup_and_audit_before_runtime_error(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    audit = AuditLog(store)
    memory = MemoryService(store, audit)
    memory.put(**_identity(), value={"generation": "old"})

    with pytest.raises(RuntimeError, match="expired during write"):
        memory.put(
            **_identity(),
            value={"generation": "expired"},
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )

    assert memory.get(**_identity()) is None
    with store.connection() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM memory_records WHERE scope = ? AND owner_id = ? "
            "AND namespace = ? AND memory_key = ?",
            ("workspace", "research", "policy", "ranking"),
        ).fetchone()[0]
    assert count == 0

    events = audit.list_for(
        entity_type="memory",
        entity_id="workspace:research:policy:ranking",
    )
    assert [event.event_type for event in events] == [
        "memory.upserted",
        "memory.upserted",
    ]


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


class _ExpiryFetchallCursor:
    def __init__(
        self,
        cursor: sqlite3.Cursor,
        selected: Event,
        writer_done: Event,
    ) -> None:
        self._cursor = cursor
        self._selected = selected
        self._writer_done = writer_done

    def fetchall(self) -> list[sqlite3.Row]:
        rows = self._cursor.fetchall()
        self._selected.set()
        if not self._writer_done.wait(timeout=5):
            raise AssertionError("concurrent memory renewal did not reach its barrier")
        return rows


class _ExpiryFetchallConnection:
    def __init__(
        self,
        conn: sqlite3.Connection,
        selected: Event,
        writer_done: Event,
        select_marker: str,
    ) -> None:
        self._conn = conn
        self._selected = selected
        self._writer_done = writer_done
        self._select_marker = select_marker

    def execute(
        self,
        sql: str,
        parameters: tuple[object, ...] = (),
    ) -> sqlite3.Cursor | _ExpiryFetchallCursor:
        cursor = self._conn.execute(sql, parameters)
        if sql.lstrip().startswith("SELECT * FROM memory_records") and (
            self._select_marker in sql
        ):
            return _ExpiryFetchallCursor(
                cursor,
                self._selected,
                self._writer_done,
            )
        return cursor


class _ExpiryFetchallStore:
    def __init__(
        self,
        store: SQLiteStore,
        selected: Event,
        writer_done: Event,
        select_marker: str,
    ) -> None:
        self._store = store
        self._selected = selected
        self._writer_done = writer_done
        self._select_marker = select_marker

    @contextmanager
    def connection(self) -> Iterator[_ExpiryFetchallConnection]:
        with self._store.connection() as conn:
            yield _ExpiryFetchallConnection(
                conn,
                self._selected,
                self._writer_done,
                self._select_marker,
            )


def _seed_expired_generation(
    store: SQLiteStore,
    memory: MemoryService,
) -> None:
    memory.put(
        **_identity(),
        value={"generation": "expired"},
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    with store.connection() as conn:
        cursor = conn.execute(
            "UPDATE memory_records SET expires_at = ? "
            "WHERE scope = ? AND owner_id = ? AND namespace = ? AND memory_key = ?",
            (
                (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                "workspace",
                "research",
                "policy",
                "ranking",
            ),
        )
        assert cursor.rowcount == 1


def test_namespace_expiry_cleanup_cannot_delete_concurrent_renewal(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    writer = MemoryService(store)
    _seed_expired_generation(store, writer)
    selected = Event()
    writer_done = Event()
    stale_reader = MemoryService(
        _ExpiryFetchallStore(
            store,
            selected,
            writer_done,
            "ORDER BY memory_key",
        )
    )  # type: ignore[arg-type]

    with ThreadPoolExecutor(max_workers=1) as pool:
        stale_read = pool.submit(
            stale_reader.list_namespace,
            scope=MemoryScope.WORKSPACE,
            owner_id="research",
            namespace="policy",
        )
        assert selected.wait(timeout=5), "namespace read never reached its barrier"
        try:
            renewed = writer.put(
                **_identity(),
                value={"generation": "renewed"},
                expires_at=datetime.now(UTC) + timedelta(hours=2),
            )
        finally:
            writer_done.set()

        assert stale_read.result(timeout=5) == ()

    durable = MemoryService(store).get(**_identity())
    assert durable == renewed
    assert durable is not None
    assert durable.value == {"generation": "renewed"}


def test_global_purge_cannot_delete_concurrent_renewal(tmp_path: Path) -> None:
    store = _store(tmp_path)
    writer = MemoryService(store)
    _seed_expired_generation(store, writer)
    selected = Event()
    writer_done = Event()
    stale_purger = MemoryService(
        _ExpiryFetchallStore(
            store,
            selected,
            writer_done,
            "WHERE expires_at IS NOT NULL",
        )
    )  # type: ignore[arg-type]

    with ThreadPoolExecutor(max_workers=1) as pool:
        purge = pool.submit(stale_purger.purge_expired)
        assert selected.wait(timeout=5), "global purge never reached its barrier"
        try:
            renewed = writer.put(
                **_identity(),
                value={"generation": "renewed"},
                expires_at=datetime.now(UTC) + timedelta(hours=2),
            )
        finally:
            writer_done.set()

        assert purge.result(timeout=5) == 0

    durable = MemoryService(store).get(**_identity())
    assert durable == renewed
    assert durable is not None
    assert durable.value == {"generation": "renewed"}
