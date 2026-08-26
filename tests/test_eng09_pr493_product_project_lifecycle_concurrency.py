from __future__ import annotations

import sqlite3
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_project import ProductProjectRepository, ProductProjectSpec
from nika_core.product_project_lifecycle import (
    ProductProjectLifecycleService,
    ProductProjectState,
)


class _DelayedReceiptCursor:
    def __init__(self, cursor: sqlite3.Cursor, *, delay_after_fetch: float) -> None:
        self._cursor = cursor
        self._delay_after_fetch = delay_after_fetch

    def fetchone(self):
        row = self._cursor.fetchone()
        time.sleep(self._delay_after_fetch)
        return row


class _DelayedReceiptConnection:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, parameters=()):
        cursor = self._conn.execute(sql, parameters)
        if "FROM product_project_mutation_idempotency" in sql:
            return _DelayedReceiptCursor(cursor, delay_after_fetch=0.15)
        return cursor


@contextmanager
def _synchronized_connections(
    store: SQLiteStore,
    barrier: threading.Barrier,
) -> Iterator[_DelayedReceiptConnection]:
    with SQLiteStore.connection(store) as conn:
        barrier.wait(timeout=5)
        yield _DelayedReceiptConnection(conn)


def test_same_key_concurrent_transition_is_one_durable_effect_and_replay(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    ProductProjectRepository(store).create(
        project_id="p1",
        name="Concurrent lifecycle project",
        spec=ProductProjectSpec(
            goal="Prove one lifecycle mutation authority",
            desired_outcome="Both same-key writers resolve to one durable transition",
        ),
        idempotency_key="create:p1",
    )

    barrier = threading.Barrier(2)

    @contextmanager
    def connection() -> Iterator[_DelayedReceiptConnection]:
        with _synchronized_connections(store, barrier) as conn:
            yield conn

    monkeypatch.setattr(store, "connection", connection)
    lifecycle = ProductProjectLifecycleService(store)

    def pause():
        return lifecycle.transition(
            "p1",
            ProductProjectState.PAUSED,
            expected_row_version=0,
            idempotency_key="status:p1:pause:concurrent",
            reason="Independent ENG09 exact-parent concurrency replay",
            changed_by_ref="qa://eng09",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(pause) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]

    assert results[0] == results[1]
    assert results[0].previous_state is ProductProjectState.ACTIVE
    assert results[0].new_state is ProductProjectState.PAUSED
    assert results[0].row_version == 1

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted_projects = ProductProjectRepository(restarted_store)
    restarted_lifecycle = ProductProjectLifecycleService(restarted_store)

    project = restarted_projects.get("p1")
    assert project.status == "paused"
    assert project.row_version == 1

    with restarted_store.connection() as conn:
        receipt_count = conn.execute(
            "SELECT COUNT(*) FROM product_project_mutation_idempotency "
            "WHERE operation_key = ?",
            ("status:p1:pause:concurrent",),
        ).fetchone()[0]
        audit_count = conn.execute(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE event_type = 'product_project.status_changed' "
            "AND entity_id = ?",
            ("p1",),
        ).fetchone()[0]

    assert receipt_count == 1
    assert audit_count == 1

    restart_replay = restarted_lifecycle.transition(
        "p1",
        ProductProjectState.PAUSED,
        expected_row_version=0,
        idempotency_key="status:p1:pause:concurrent",
        reason="Independent ENG09 exact-parent concurrency replay",
        changed_by_ref="qa://eng09",
    )
    assert restart_replay == results[0]
