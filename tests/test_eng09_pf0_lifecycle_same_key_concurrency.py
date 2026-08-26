from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_project import ProductProjectRepository, ProductProjectSpec
from nika_core.product_project_lifecycle import (
    ProductProjectLifecycleService,
    ProductProjectState,
)


class _ReadGateCursor:
    def __init__(self, cursor: Any, connection: Any, barrier: threading.Barrier) -> None:
        self._cursor = cursor
        self._connection = connection
        self._barrier = barrier

    def fetchone(self) -> Any:
        row = self._cursor.fetchone()
        if not self._connection.in_transaction:
            self._barrier.wait(timeout=10)
        return row

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


class _ReadGateConnection:
    def __init__(self, connection: Any, barrier: threading.Barrier) -> None:
        self._connection = connection
        self._barrier = barrier

    def execute(self, sql: str, parameters: Any = ()) -> Any:
        cursor = self._connection.execute(sql, parameters)
        normalized = " ".join(sql.split()).casefold()
        if normalized.startswith(
            "select status,row_version from product_projects where project_id=?"
        ):
            return _ReadGateCursor(cursor, self._connection, self._barrier)
        return cursor

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def test_same_key_concurrent_lifecycle_transition_is_one_effect_and_replay_equivalent(
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
            goal="Serialize lifecycle authority",
            desired_outcome="One durable effect with replay-equivalent callers",
        ),
        idempotency_key="create:p-eng09-lifecycle-race",
    )

    original_connection = SQLiteStore.connection
    stale_read_barrier = threading.Barrier(2)

    @contextmanager
    def gated_connection(self):
        with original_connection(self) as connection:
            yield _ReadGateConnection(connection, stale_read_barrier)

    monkeypatch.setattr(SQLiteStore, "connection", gated_connection)
    start_barrier = threading.Barrier(2)

    def transition():
        start_barrier.wait(timeout=10)
        return ProductProjectLifecycleService(SQLiteStore(db_path)).transition(
            "p-eng09-lifecycle-race",
            ProductProjectState.PAUSED,
            expected_row_version=0,
            idempotency_key="status:p-eng09-lifecycle-race:pause",
            reason="Deterministic concurrent same-key retry",
            changed_by_ref="qa://eng09",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(transition) for _ in range(2)]
        results = [future.result(timeout=15) for future in futures]

    assert results[0] == results[1]
    assert results[0].row_version == 1
    assert results[0].previous_state is ProductProjectState.ACTIVE
    assert results[0].new_state is ProductProjectState.PAUSED

    monkeypatch.setattr(SQLiteStore, "connection", original_connection)
    restarted_store = SQLiteStore(db_path)
    restarted_store.initialize()
    restarted_projects = ProductProjectRepository(restarted_store)
    restarted_lifecycle = ProductProjectLifecycleService(restarted_store)

    project = restarted_projects.get("p-eng09-lifecycle-race")
    assert project.status == "paused"
    assert project.row_version == 1
    with restarted_store.connection() as connection:
        effect_count = connection.execute(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE event_type='product_project.status_changed' "
            "AND entity_type='product_project' AND entity_id=?",
            ("p-eng09-lifecycle-race",),
        ).fetchone()[0]
        idempotency_count = connection.execute(
            "SELECT COUNT(*) FROM product_project_mutation_idempotency "
            "WHERE operation_key=?",
            ("status:p-eng09-lifecycle-race:pause",),
        ).fetchone()[0]
    assert effect_count == 1
    assert idempotency_count == 1

    replay = restarted_lifecycle.transition(
        "p-eng09-lifecycle-race",
        ProductProjectState.PAUSED,
        expected_row_version=0,
        idempotency_key="status:p-eng09-lifecycle-race:pause",
        reason="Deterministic concurrent same-key retry",
        changed_by_ref="qa://eng09",
    )
    assert replay == results[0]
