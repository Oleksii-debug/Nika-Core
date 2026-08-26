from __future__ import annotations

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_project import ProductProjectRepository, ProductProjectSpec
from nika_core.product_project_lifecycle import (
    ProductProjectLifecycleService,
    ProductProjectState,
)


class _CursorProxy:
    def __init__(self, cursor: Any, barrier: threading.Barrier) -> None:
        self._cursor = cursor
        self._barrier = barrier

    def fetchone(self) -> Any:
        row = self._cursor.fetchone()
        self._barrier.wait(timeout=10)
        return row

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


class _ConnectionProxy:
    def __init__(
        self,
        conn: Any,
        *,
        coordinate_unreserved_reads: threading.Event,
        receipt_barrier: threading.Barrier,
        state_barrier: threading.Barrier,
    ) -> None:
        self._conn = conn
        self._coordinate_unreserved_reads = coordinate_unreserved_reads
        self._receipt_barrier = receipt_barrier
        self._state_barrier = state_barrier

    def execute(self, statement: str, parameters: Any = ()) -> Any:
        normalized = " ".join(statement.split())
        barrier: threading.Barrier | None = None
        if self._coordinate_unreserved_reads.is_set() and not self._conn.in_transaction:
            if "FROM product_project_mutation_idempotency" in normalized:
                barrier = self._receipt_barrier
            elif "SELECT status,row_version FROM product_projects" in normalized:
                barrier = self._state_barrier

        cursor = self._conn.execute(statement, parameters)
        if barrier is not None:
            return _CursorProxy(cursor, barrier)
        return cursor

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def test_same_key_concurrent_transition_is_one_effect_and_durable_replay(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "nika.db"
    store = SQLiteStore(db_path)
    store.initialize()
    ProductProjectRepository(store).create(
        project_id="p-eng09-lifecycle-race",
        name="ENG09 lifecycle race",
        spec=ProductProjectSpec(
            goal="Serialize lifecycle mutation authority",
            desired_outcome="One durable effect and equivalent replay for same-key writers",
        ),
        idempotency_key="create:p-eng09-lifecycle-race",
    )

    original_connection = SQLiteStore.connection
    coordinate_unreserved_reads = threading.Event()
    coordinate_unreserved_reads.set()
    receipt_barrier = threading.Barrier(2)
    state_barrier = threading.Barrier(2)

    @contextmanager
    def coordinated_connection(self: SQLiteStore) -> Iterator[Any]:
        with original_connection(self) as conn:
            yield _ConnectionProxy(
                conn,
                coordinate_unreserved_reads=coordinate_unreserved_reads,
                receipt_barrier=receipt_barrier,
                state_barrier=state_barrier,
            )

    monkeypatch.setattr(SQLiteStore, "connection", coordinated_connection)
    start = threading.Barrier(2)

    def transition_once():
        start.wait(timeout=10)
        return ProductProjectLifecycleService(SQLiteStore(db_path)).transition(
            "p-eng09-lifecycle-race",
            ProductProjectState.PAUSED,
            expected_row_version=0,
            idempotency_key="status:p-eng09-lifecycle-race:pause",
            reason="Independent same-key concurrency replay",
            changed_by_ref="qa://eng09",
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(transition_once) for _ in range(2)]
            results = [future.result(timeout=20) for future in futures]
    finally:
        coordinate_unreserved_reads.clear()

    assert results[0] == results[1]
    assert results[0].row_version == 1
    assert results[0].previous_state is ProductProjectState.ACTIVE
    assert results[0].new_state is ProductProjectState.PAUSED

    restarted_store = SQLiteStore(db_path)
    restarted_project = ProductProjectRepository(restarted_store).get("p-eng09-lifecycle-race")
    assert restarted_project.status == ProductProjectState.PAUSED.value
    assert restarted_project.row_version == 1

    restarted = ProductProjectLifecycleService(restarted_store)
    history = restarted.history("p-eng09-lifecycle-race")
    assert len(history) == 2
    assert history[-1] == results[0]

    replay = restarted.transition(
        "p-eng09-lifecycle-race",
        ProductProjectState.PAUSED,
        expected_row_version=0,
        idempotency_key="status:p-eng09-lifecycle-race:pause",
        reason="Independent same-key concurrency replay",
        changed_by_ref="qa://eng09",
    )
    assert replay == results[0]
