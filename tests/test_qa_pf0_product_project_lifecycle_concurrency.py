from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_project import ProductProjectRepository, ProductProjectSpec
from nika_core.product_project_lifecycle import (
    ProductProjectLifecycleService,
    ProductProjectState,
    ProductProjectStatusTransition,
)


def test_same_key_concurrent_transition_replays_one_durable_effect(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    ProductProjectRepository(store).create(
        project_id="p1",
        name="Concurrent lifecycle authority",
        spec=ProductProjectSpec(
            goal="Serialize same-key lifecycle writers",
            desired_outcome="One durable transition with replay-equivalent results",
        ),
        idempotency_key="create:p1",
    )

    start = Barrier(2)

    def transition() -> ProductProjectStatusTransition:
        lifecycle = ProductProjectLifecycleService(store)
        start.wait(timeout=5)
        return lifecycle.transition(
            "p1",
            ProductProjectState.PAUSED,
            expected_row_version=0,
            idempotency_key="status:p1:pause",
            reason="Independent W008 concurrent replay proof",
            changed_by_ref="qa://c10-pf0-493",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = tuple(executor.submit(transition) for _ in range(2))
        results = tuple(future.result(timeout=10) for future in futures)

    assert results[0] == results[1]
    assert results[0].row_version == 1
    assert results[0].previous_state is ProductProjectState.ACTIVE
    assert results[0].new_state is ProductProjectState.PAUSED

    restarted_store = SQLiteStore(store.path)
    restarted = ProductProjectLifecycleService(restarted_store)
    durable_project = ProductProjectRepository(restarted_store).get("p1")
    assert durable_project.status == ProductProjectState.PAUSED.value
    assert durable_project.row_version == 1
    assert restarted.current_state("p1") is ProductProjectState.PAUSED

    history = restarted.history("p1")
    assert len(history) == 2
    assert history[1] == results[0]

    replay = restarted.transition(
        "p1",
        ProductProjectState.PAUSED,
        expected_row_version=0,
        idempotency_key="status:p1:pause",
        reason="Independent W008 concurrent replay proof",
        changed_by_ref="qa://c10-pf0-493",
    )
    assert replay == results[0]

    with restarted_store.connection() as conn:
        receipts = conn.execute(
            "SELECT entity_version FROM product_project_mutation_idempotency "
            "WHERE operation_key=?",
            ("status:p1:pause",),
        ).fetchall()
        audit_count = conn.execute(
            "SELECT COUNT(*) FROM audit_events "
            "WHERE event_type='product_project.status_changed' "
            "AND entity_type='product_project' AND entity_id=?",
            ("p1",),
        ).fetchone()[0]

    assert len(receipts) == 1
    assert receipts[0]["entity_version"] == 1
    assert audit_count == 1
