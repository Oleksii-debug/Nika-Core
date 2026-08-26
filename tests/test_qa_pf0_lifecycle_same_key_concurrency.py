from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_project import ProductProjectRepository, ProductProjectSpec
from nika_core.product_project_lifecycle import (
    ProductProjectLifecycleService,
    ProductProjectState,
)


def test_same_key_concurrent_transition_is_one_effect_and_durable_replay(tmp_path) -> None:
    """Replay the W008 lifecycle race against the exact #493 production parent."""
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    ProductProjectRepository(store).create(
        project_id="p1",
        name="PF0 lifecycle authority",
        spec=ProductProjectSpec(
            goal="Serialize identical lifecycle mutations",
            desired_outcome="One durable effect and equivalent replay for both callers",
        ),
        idempotency_key="create:p1",
    )

    # Make the old read-before-write race deterministic without slowing the repaired shape.
    # If the mutation-receipt SELECT runs outside a writer transaction, both callers are
    # forced to observe the same pre-write authority state before either may continue.
    # On #493, BEGIN IMMEDIATE has already set conn.in_transaction, so this gate is skipped.
    receipt_gate = threading.Barrier(2)
    original_connection = store.connection

    @contextmanager
    def gated_connection():
        with original_connection() as conn:
            def trace(statement: str) -> None:
                if "FROM product_project_mutation_idempotency" not in statement:
                    return
                if conn.in_transaction:
                    return
                receipt_gate.wait(timeout=5.0)

            conn.set_trace_callback(trace)
            try:
                yield conn
            finally:
                conn.set_trace_callback(None)

    store.connection = gated_connection  # type: ignore[method-assign]
    start_gate = threading.Barrier(2)

    def transition():
        start_gate.wait(timeout=5.0)
        return ProductProjectLifecycleService(store).transition(
            "p1",
            ProductProjectState.PAUSED,
            expected_row_version=0,
            idempotency_key="status:p1:pause",
            reason="Independent exact-parent PF0 concurrency replay",
            changed_by_ref="qa://pf0-w008-replay",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(transition) for _ in range(2)]
        first = futures[0].result(timeout=10.0)
        second = futures[1].result(timeout=10.0)

    assert first == second
    assert first.row_version == 1
    assert first.previous_state is ProductProjectState.ACTIVE
    assert first.new_state is ProductProjectState.PAUSED

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    restarted = ProductProjectLifecycleService(restarted_store)
    replay = restarted.transition(
        "p1",
        ProductProjectState.PAUSED,
        expected_row_version=0,
        idempotency_key="status:p1:pause",
        reason="Independent exact-parent PF0 concurrency replay",
        changed_by_ref="qa://pf0-w008-replay",
    )
    assert replay == first
    assert restarted.current_state("p1") is ProductProjectState.PAUSED
    assert restarted.projects.get("p1").row_version == 1
    history = restarted.history("p1")
    assert len(history) == 2
    assert history[1] == first

    with restarted_store.connection() as conn:
        receipt_count = conn.execute(
            "SELECT COUNT(*) FROM product_project_mutation_idempotency "
            "WHERE operation_key='status:p1:pause'"
        ).fetchone()[0]
        audit_count = conn.execute(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE event_type='product_project.status_changed' AND entity_id='p1'"
        ).fetchone()[0]

    assert receipt_count == 1
    assert audit_count == 1
