from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event

from nika_core.data.sqlite import SQLiteStore
from nika_core.memory import MemoryRecord, MemoryScope, MemoryService


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
            assert self._release.wait(timeout=5), "writer A was not released"


def test_put_returns_value_committed_by_that_writer_if_newer_write_wins_before_return(
    tmp_path: Path,
) -> None:
    path = tmp_path / "nika.db"
    store = SQLiteStore(path)
    store.initialize()
    a_committed = Event()
    release_a = Event()
    writer_a = MemoryService(
        _PauseAfterCommitSQLiteStore(path, committed=a_committed, release=release_a)
    )
    writer_b = MemoryService(SQLiteStore(path))

    def put_a() -> MemoryRecord:
        return writer_a.put(
            scope=MemoryScope.WORKSPACE,
            owner_id="workspace-a",
            namespace="shared",
            key="same-key",
            value={"writer": "a"},
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future_a = pool.submit(put_a)
        assert a_committed.wait(timeout=5), "writer A did not commit"
        returned_b = writer_b.put(
            scope=MemoryScope.WORKSPACE,
            owner_id="workspace-a",
            namespace="shared",
            key="same-key",
            value={"writer": "b"},
        )
        release_a.set()
        returned_a = future_a.result(timeout=5)

    assert returned_a.value == {"writer": "a"}
    assert returned_b.value == {"writer": "b"}
    current = writer_b.get(
        scope=MemoryScope.WORKSPACE,
        owner_id="workspace-a",
        namespace="shared",
        key="same-key",
    )
    assert current is not None
    assert current.value == {"writer": "b"}
