from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime as RealDateTime, timedelta
from pathlib import Path
from threading import Event
from typing import Any

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.memory import MemoryScope, MemoryService
import nika_core.memory.service as memory_service


class FakeDateTime(RealDateTime):
    _current = RealDateTime(2035, 1, 1, 12, 0, tzinfo=UTC)

    @classmethod
    def set(cls, value: RealDateTime) -> None:
        cls._current = value.astimezone(UTC)

    @classmethod
    def now(cls, tz: Any = None) -> RealDateTime:
        current = cls._current
        if tz is None:
            return current.replace(tzinfo=None)
        return current.astimezone(tz)


@pytest.fixture()
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> type[FakeDateTime]:
    FakeDateTime.set(RealDateTime(2035, 1, 1, 12, 0, tzinfo=UTC))
    monkeypatch.setattr(memory_service, "datetime", FakeDateTime)
    return FakeDateTime


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    return store


def _identity(key: str = "fact") -> dict[str, object]:
    return {
        "scope": MemoryScope.WORKSPACE,
        "owner_id": "project-1",
        "namespace": "planning",
        "key": key,
    }


def test_expiry_boundary_and_clock_rollback_are_deterministic(
    tmp_path: Path,
    fake_clock: type[FakeDateTime],
) -> None:
    store = _store(tmp_path)
    memory = MemoryService(store)
    expiry = RealDateTime(2035, 1, 1, 12, 1, tzinfo=UTC)

    fake_clock.set(expiry - timedelta(seconds=1))
    created = memory.put(
        **_identity(),
        value={"status": "active"},
        expires_at=expiry,
    )
    assert created.expires_at == expiry

    fake_clock.set(expiry - timedelta(microseconds=1))
    before = memory.get(**_identity())
    assert before is not None
    assert before.value == {"status": "active"}

    fake_clock.set(expiry)
    assert memory.get(**_identity()) is None

    fake_clock.set(expiry + timedelta(days=1))
    assert memory.get(**_identity()) is None

    # Once expiration has been observed and cleanup committed, wall-clock rollback cannot
    # resurrect the old durable value, including after a process-style service restart.
    fake_clock.set(expiry - timedelta(days=1))
    restarted = MemoryService(store)
    assert restarted.get(**_identity()) is None


def test_active_retrieval_excludes_records_at_exact_expiry(
    tmp_path: Path,
    fake_clock: type[FakeDateTime],
) -> None:
    memory = MemoryService(_store(tmp_path))
    expiry = RealDateTime(2035, 1, 1, 12, 1, tzinfo=UTC)

    fake_clock.set(expiry - timedelta(seconds=1))
    memory.put(**_identity("expires-now"), value="stale", expires_at=expiry)
    memory.put(
        **_identity("still-live"),
        value="usable",
        expires_at=expiry + timedelta(minutes=1),
    )

    fake_clock.set(expiry - timedelta(microseconds=1))
    before = memory.list_namespace(
        scope=MemoryScope.WORKSPACE,
        owner_id="project-1",
        namespace="planning",
    )
    assert [(record.key, record.value) for record in before] == [
        ("expires-now", "stale"),
        ("still-live", "usable"),
    ]

    fake_clock.set(expiry)
    exact = memory.list_namespace(
        scope=MemoryScope.WORKSPACE,
        owner_id="project-1",
        namespace="planning",
    )
    assert [(record.key, record.value) for record in exact] == [("still-live", "usable")]

    fake_clock.set(expiry + timedelta(minutes=1))
    assert memory.list_namespace(
        scope=MemoryScope.WORKSPACE,
        owner_id="project-1",
        namespace="planning",
    ) == ()


class GatedCursor:
    def __init__(self, cursor: sqlite3.Cursor, selected: Event, writer_done: Event) -> None:
        self._cursor = cursor
        self._selected = selected
        self._writer_done = writer_done

    def fetchone(self) -> sqlite3.Row | None:
        row = self._cursor.fetchone()
        # Exhaust the one-row SELECT before opening the deterministic interleaving window so
        # this oracle does not depend on sleep timing or on holding a SQLite read statement.
        self._cursor.fetchall()
        self._selected.set()
        if not self._writer_done.wait(timeout=5):
            raise AssertionError("concurrent renewal did not reach its deterministic barrier")
        return row


class GatedConnection:
    def __init__(self, conn: sqlite3.Connection, selected: Event, writer_done: Event) -> None:
        self._conn = conn
        self._selected = selected
        self._writer_done = writer_done

    def execute(
        self,
        sql: str,
        parameters: tuple[object, ...] = (),
    ) -> sqlite3.Cursor | GatedCursor:
        cursor = self._conn.execute(sql, parameters)
        if sql.lstrip().startswith("SELECT * FROM memory_records WHERE scope = ?"):
            return GatedCursor(cursor, self._selected, self._writer_done)
        return cursor


class GatedStore:
    def __init__(self, store: SQLiteStore, selected: Event, writer_done: Event) -> None:
        self._store = store
        self._selected = selected
        self._writer_done = writer_done

    @contextmanager
    def connection(self) -> Iterator[GatedConnection]:
        with self._store.connection() as conn:
            yield GatedConnection(conn, self._selected, self._writer_done)


def test_stale_lazy_cleanup_cannot_delete_concurrent_renewal(
    tmp_path: Path,
    fake_clock: type[FakeDateTime],
) -> None:
    store = _store(tmp_path)
    expiry = RealDateTime(2035, 1, 1, 12, 1, tzinfo=UTC)
    fake_clock.set(expiry - timedelta(seconds=1))
    MemoryService(store).put(
        **_identity(),
        value={"generation": "expired"},
        expires_at=expiry,
    )

    selected = Event()
    writer_done = Event()
    stale_reader = MemoryService(GatedStore(store, selected, writer_done))  # type: ignore[arg-type]
    writer = MemoryService(store)

    fake_clock.set(expiry)
    with ThreadPoolExecutor(max_workers=1) as pool:
        stale_read = pool.submit(stale_reader.get, **_identity())
        assert selected.wait(timeout=5), "expired read never reached deterministic barrier"

        fake_clock.set(expiry + timedelta(seconds=1))
        try:
            renewed = writer.put(
                **_identity(),
                value={"generation": "renewed"},
                expires_at=expiry + timedelta(hours=1),
            )
        finally:
            writer_done.set()

        assert renewed.value == {"generation": "renewed"}
        assert stale_read.result(timeout=5) is None

    durable = MemoryService(store).get(**_identity())
    assert durable is not None
    assert durable.value == {"generation": "renewed"}
    assert durable.expires_at == expiry + timedelta(hours=1)
