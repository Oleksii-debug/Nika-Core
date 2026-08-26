from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier
from typing import Any

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_project import ProductProjectRepository, ProductProjectSpec
from nika_core.product_project_lifecycle import (
    ProductProjectLifecycleService,
    ProductProjectState,
)


class _ContestedConnection:
    """Force pre-write lifecycle reads to overlap without changing production code."""

    def __init__(
        self,
        conn: Any,
        receipt_reads: Barrier,
        project_reads: Barrier,
    ) -> None:
        self._conn = conn
        self._receipt_reads = receipt_reads
        self._project_reads = project_reads

    def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> Any:
        cursor = self._conn.execute(sql, parameters)
        statement = " ".join(sql.split()).casefold()

        # On the unfixed parent, SELECTs happen before any explicit writer
        # reservation, so both callers are forced to observe the same durable
        # pre-state before either may write. On the candidate, BEGIN IMMEDIATE
        # makes in_transaction true first, so the barriers are intentionally
        # bypassed and SQLite itself serializes the authority interval.
        if not self._conn.in_transaction:
            if "from product_project_mutation_idempotency" in statement:
                self._receipt_reads.wait(timeout=5)
            elif "from product_projects where project_id=?" in statement:
                self._project_reads.wait(timeout=5)
        return cursor

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def test_same_key_concurrent_transition_is_one_effect_and_restart_replay(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    ProductProjectRepository(store).create(
        project_id="p1",
        name="Concurrent lifecycle authority",
        spec=ProductProjectSpec(
            goal="Serialize same-key lifecycle mutation",
            desired_outcome="One durable effect with equivalent replay results",
        ),
        idempotency_key="create:p1",
    )

    receipt_reads = Barrier(2)
    project_reads = Barrier(2)
    worker_start = Barrier(2)
    original_connection = store.connection

    @contextmanager
    def contested_connection():
        with original_connection() as conn:
            yield _ContestedConnection(conn, receipt_reads, project_reads)

    store.connection = contested_connection  # type: ignore[method-assign]
    first_service = ProductProjectLifecycleService(store)
    second_service = ProductProjectLifecycleService(store)

    def transition(service: ProductProjectLifecycleService):
        worker_start.wait(timeout=5)
        return service.transition(
            "p1",
            ProductProjectState.PAUSED,
            expected_row_version=0,
            idempotency_key="status:p1:pause:concurrent",
            reason="Concurrent deterministic lifecycle replay",
            changed_by_ref="agent://eng09",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(transition, first_service)
        second_future = executor.submit(transition, second_service)
        first = first_future.result(timeout=15)
        second = second_future.result(timeout=15)

    assert first == second
    assert first.row_version == 1
    assert first.new_state is ProductProjectState.PAUSED

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted = ProductProjectLifecycleService(restarted_store)
    replay = restarted.transition(
        "p1",
        ProductProjectState.PAUSED,
        expected_row_version=0,
        idempotency_key="status:p1:pause:concurrent",
        reason="Concurrent deterministic lifecycle replay",
        changed_by_ref="agent://eng09",
    )

    assert replay == first
    assert restarted.projects.get("p1").row_version == 1
    assert restarted.current_state("p1") is ProductProjectState.PAUSED
    history = restarted.history("p1")
    assert len(history) == 2
    assert history[1] == first

    with restarted_store.connection() as conn:
        receipt_count = conn.execute(
            "SELECT COUNT(*) FROM product_project_mutation_idempotency "
            "WHERE operation_key=?",
            ("status:p1:pause:concurrent",),
        ).fetchone()[0]
        event_count = conn.execute(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE event_type='product_project.status_changed' "
            "AND entity_type='product_project' AND entity_id='p1'"
        ).fetchone()[0]

    assert receipt_count == 1
    assert event_count == 1
