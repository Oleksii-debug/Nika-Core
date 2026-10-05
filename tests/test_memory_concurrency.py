from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Event

import pytest

import nika_core.memory.service as memory_service_module
from nika_core.data.sqlite import SQLiteStore
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


def test_unconditional_put_remains_compatible_and_revision_is_monotonic(tmp_path: Path) -> None:
    memory = MemoryService(_store(tmp_path))
    first = memory.put(**_identity(), value={"mode": "stable"})
    second = memory.put(**_identity(), value={"mode": "new"})

    assert second.value == {"mode": "new"}
    assert second.created_at == first.created_at
    assert second.updated_at > first.updated_at


def test_compare_and_put_rejects_stale_revision_after_restart(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first_service = MemoryService(store)
    original = first_service.compare_and_put(
        **_identity(),
        value={"mode": "initial"},
        expected_updated_at=None,
    )

    restarted = MemoryService(store)
    observed = restarted.get(**_identity())
    assert observed == original

    winner = first_service.compare_and_put(
        **_identity(),
        value={"mode": "winner"},
        expected_updated_at=original.updated_at,
    )
    with pytest.raises(MemoryConflictError, match="revision changed"):
        restarted.compare_and_put(
            **_identity(),
            value={"mode": "stale"},
            expected_updated_at=observed.updated_at,
        )

    durable = MemoryService(store).get(**_identity())
    assert durable == winner
    assert durable is not None and durable.value == {"mode": "winner"}


def test_concurrent_create_binds_absent_identity_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    barrier = Barrier(2)

    def create(value: str) -> str:
        service = MemoryService(store)
        barrier.wait()
        try:
            service.compare_and_put(
                **_identity(),
                value={"writer": value},
                expected_updated_at=None,
            )
        except MemoryConflictError:
            return "conflict"
        return "created"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(create, ("a", "b")))

    assert sorted(results) == ["conflict", "created"]
    durable = MemoryService(store).get(**_identity())
    assert durable is not None
    assert durable.value in ({"writer": "a"}, {"writer": "b"})


def test_compare_and_delete_rejects_stale_writer(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first_service = MemoryService(store)
    original = first_service.put(**_identity(), value={"mode": "initial"})
    winner = first_service.compare_and_put(
        **_identity(),
        value={"mode": "winner"},
        expected_updated_at=original.updated_at,
    )

    restarted = MemoryService(store)
    with pytest.raises(MemoryConflictError, match="revision changed"):
        restarted.compare_and_delete(
            **_identity(),
            expected_updated_at=original.updated_at,
        )

    assert restarted.get(**_identity()) == winner
    assert restarted.compare_and_delete(
        **_identity(),
        expected_updated_at=winner.updated_at,
    )
    assert restarted.get(**_identity()) is None


def test_conditional_user_memory_still_requires_explicit_approval(tmp_path: Path) -> None:
    memory = MemoryService(_store(tmp_path))
    with pytest.raises(PermissionError):
        memory.compare_and_put(
            scope=MemoryScope.USER,
            owner_id="local-user",
            namespace="preferences",
            key="language",
            value="uk",
            expected_updated_at=None,
        )


def test_compare_and_put_can_replace_logically_expired_record(tmp_path: Path) -> None:
    store = _store(tmp_path)
    memory = MemoryService(store)
    memory.put(
        **_identity(),
        value={"mode": "expired"},
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    expired_at = datetime.now(UTC) - timedelta(seconds=1)
    with store.connection() as conn:
        cursor = conn.execute(
            "UPDATE memory_records SET expires_at = ? "
            "WHERE scope = ? AND owner_id = ? AND namespace = ? AND memory_key = ?",
            (
                expired_at.isoformat(),
                MemoryScope.WORKSPACE.value,
                "research",
                "policy",
                "ranking",
            ),
        )
        assert cursor.rowcount == 1

    replacement = memory.compare_and_put(
        **_identity(),
        value={"mode": "replacement"},
        expected_updated_at=None,
    )

    assert replacement.value == {"mode": "replacement"}
    assert replacement.expires_at is None


class _GatedCursor:
    def __init__(self, cursor: sqlite3.Cursor, selected: Event, writer_done: Event) -> None:
        self._cursor = cursor
        self._selected = selected
        self._writer_done = writer_done

    def fetchone(self) -> sqlite3.Row | None:
        row = self._cursor.fetchone()
        self._cursor.fetchall()
        self._selected.set()
        if not self._writer_done.wait(timeout=5):
            raise AssertionError("concurrent memory renewal did not reach its barrier")
        return row


class _GatedConnection:
    def __init__(self, conn: sqlite3.Connection, selected: Event, writer_done: Event) -> None:
        self._conn = conn
        self._selected = selected
        self._writer_done = writer_done

    def execute(
        self,
        sql: str,
        parameters: tuple[object, ...] = (),
    ) -> sqlite3.Cursor | _GatedCursor:
        cursor = self._conn.execute(sql, parameters)
        if sql.lstrip().startswith("SELECT * FROM memory_records WHERE scope = ?"):
            return _GatedCursor(cursor, self._selected, self._writer_done)
        return cursor


class _GatedStore:
    def __init__(self, store: SQLiteStore, selected: Event, writer_done: Event) -> None:
        self._store = store
        self._selected = selected
        self._writer_done = writer_done

    @contextmanager
    def connection(self) -> Iterator[_GatedConnection]:
        with self._store.connection() as conn:
            yield _GatedConnection(conn, self._selected, self._writer_done)


def test_stale_expiry_cleanup_cannot_delete_concurrent_renewal(tmp_path: Path) -> None:
    store = _store(tmp_path)
    memory = MemoryService(store)
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
                MemoryScope.WORKSPACE.value,
                "research",
                "policy",
                "ranking",
            ),
        )
        assert cursor.rowcount == 1

    selected = Event()
    writer_done = Event()
    stale_reader = MemoryService(_GatedStore(store, selected, writer_done))  # type: ignore[arg-type]
    writer = MemoryService(store)

    with ThreadPoolExecutor(max_workers=1) as pool:
        stale_read = pool.submit(stale_reader.get, **_identity())
        assert selected.wait(timeout=5), "expired read never reached its deterministic barrier"
        try:
            renewed = writer.put(
                **_identity(),
                value={"generation": "renewed"},
                expires_at=datetime.now(UTC) + timedelta(hours=2),
            )
        finally:
            writer_done.set()

        assert renewed.value == {"generation": "renewed"}
        assert stale_read.result(timeout=5) is None

    durable = MemoryService(store).get(**_identity())
    assert durable == renewed
    assert durable is not None and durable.value == {"generation": "renewed"}


class _PauseAfterCommitSQLiteStore(SQLiteStore):
    def __init__(self, path: Path, *, committed: Event, release: Event) -> None:
        super().__init__(path)
        self._committed = committed
        self._release = release
        self._pause_next = True

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        with super().connection() as conn:
            yield conn
        if self._pause_next:
            self._pause_next = False
            self._committed.set()
            if not self._release.wait(timeout=5):
                raise AssertionError("writer A was not released after commit")


def test_compare_and_put_returns_exact_committed_revision_after_later_overwrite(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    baseline_service = MemoryService(store)
    baseline = baseline_service.compare_and_put(
        **_identity(),
        value={"writer": "baseline"},
        expected_updated_at=None,
    )

    a_committed = Event()
    release_a = Event()
    writer_a = MemoryService(
        _PauseAfterCommitSQLiteStore(
            store.path,
            committed=a_committed,
            release=release_a,
        )
    )
    writer_b = MemoryService(store)

    def commit_a():
        return writer_a.compare_and_put(
            **_identity(),
            value={"writer": "A"},
            expected_updated_at=baseline.updated_at,
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future_a = pool.submit(commit_a)
        assert a_committed.wait(timeout=5), "writer A did not commit"

        observed_a = writer_b.get(**_identity())
        assert observed_a is not None
        assert observed_a.value == {"writer": "A"}
        durable_b = writer_b.compare_and_put(
            **_identity(),
            value={"writer": "B"},
            expected_updated_at=observed_a.updated_at,
        )
        release_a.set()
        returned_a = future_a.result(timeout=5)

    assert returned_a.value == {"writer": "A"}
    assert returned_a.updated_at == observed_a.updated_at
    assert durable_b.value == {"writer": "B"}
    assert durable_b.updated_at > returned_a.updated_at
    assert MemoryService(store).get(**_identity()) == durable_b


class _GateBeforeImmediateConnection:
    def __init__(
        self,
        conn: sqlite3.Connection,
        reached: Event,
        release: Event,
    ) -> None:
        self._conn = conn
        self._reached = reached
        self._release = release

    def execute(
        self,
        sql: str,
        parameters: tuple[object, ...] = (),
    ) -> sqlite3.Cursor:
        if sql.strip().upper() == "BEGIN IMMEDIATE":
            self._reached.set()
            if not self._release.wait(timeout=5):
                raise AssertionError("unconditional writer was not released")
        return self._conn.execute(sql, parameters)


class _GateBeforeImmediateStore(SQLiteStore):
    def __init__(
        self,
        path: Path,
        *,
        reached: Event,
        release: Event,
    ) -> None:
        super().__init__(path)
        self._reached = reached
        self._release = release

    @contextmanager
    def connection(self) -> Iterator[_GateBeforeImmediateConnection]:
        with super().connection() as conn:
            yield _GateBeforeImmediateConnection(
                conn,
                self._reached,
                self._release,
            )


class _FrozenDateTime(datetime):
    fixed = datetime(2026, 10, 5, 7, 0, 0, tzinfo=UTC)

    @classmethod
    def now(cls, tz=None):  # type: ignore[no-untyped-def]
        if tz is None:
            return cls.fixed.replace(tzinfo=None)
        return cls.fixed.astimezone(tz)


def test_unconditional_put_serializes_revision_after_cas_winner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(memory_service_module, "datetime", _FrozenDateTime)

    store = _store(tmp_path)
    service = MemoryService(store)
    baseline = service.compare_and_put(
        **_identity(),
        value={"writer": "baseline"},
        expected_updated_at=None,
    )

    begin_reached = Event()
    release_unconditional = Event()
    unconditional = MemoryService(
        _GateBeforeImmediateStore(
            store.path,
            reached=begin_reached,
            release=release_unconditional,
        )
    )

    def legacy_put():
        return unconditional.put(
            **_identity(),
            value={"writer": "unconditional"},
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future_unconditional = pool.submit(legacy_put)
        assert begin_reached.wait(timeout=5), (
            "unconditional writer did not enter the shared write boundary"
        )

        winner = service.compare_and_put(
            **_identity(),
            value={"writer": "cas"},
            expected_updated_at=baseline.updated_at,
        )
        assert winner.value == {"writer": "cas"}

        release_unconditional.set()
        legacy_result = future_unconditional.result(timeout=5)

    final = MemoryService(store).get(**_identity())
    assert final is not None
    assert final.value == {"writer": "unconditional"}
    assert legacy_result == final
    assert final.updated_at > winner.updated_at

    with pytest.raises(MemoryConflictError, match="revision changed"):
        MemoryService(store).compare_and_put(
            **_identity(),
            value={"writer": "stale-caller"},
            expected_updated_at=winner.updated_at,
        )
