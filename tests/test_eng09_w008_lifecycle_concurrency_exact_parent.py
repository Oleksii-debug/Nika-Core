from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_project import ProductProjectRepository, ProductProjectSpec
from nika_core.product_project_lifecycle import (
    ProductProjectLifecycleService,
    ProductProjectState,
)


def test_identical_concurrent_transition_replays_one_durable_effect(tmp_path) -> None:
    db_path = tmp_path / "nika.db"
    store = SQLiteStore(db_path)
    store.initialize()
    ProductProjectRepository(store).create(
        project_id="p-w008-lifecycle-concurrency",
        name="W008 lifecycle concurrency",
        spec=ProductProjectSpec(
            goal="Serialize identical lifecycle transitions",
            desired_outcome="One durable effect with equivalent replay results",
        ),
        idempotency_key="create:p-w008-lifecycle-concurrency",
    )

    gate = threading.Barrier(3)

    def pause_once():
        gate.wait(timeout=10)
        return ProductProjectLifecycleService(SQLiteStore(db_path)).transition(
            "p-w008-lifecycle-concurrency",
            ProductProjectState.PAUSED,
            expected_row_version=0,
            idempotency_key="status:p-w008-lifecycle-concurrency:pause",
            reason="W008 exact-parent concurrent replay proof",
            changed_by_ref="qa://eng09-w008",
        )

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="w008-transition") as executor:
        futures = [executor.submit(pause_once) for _ in range(2)]
        gate.wait(timeout=10)
        results = [future.result(timeout=10) for future in futures]

    assert results[0] == results[1]
    assert results[0].row_version == 1
    assert results[0].previous_state is ProductProjectState.ACTIVE
    assert results[0].new_state is ProductProjectState.PAUSED

    restarted = ProductProjectLifecycleService(SQLiteStore(db_path))
    assert restarted.current_state("p-w008-lifecycle-concurrency") is ProductProjectState.PAUSED
    history = restarted.history("p-w008-lifecycle-concurrency")
    assert len(history) == 2
    assert history[1] == results[0]

    replay = restarted.transition(
        "p-w008-lifecycle-concurrency",
        ProductProjectState.PAUSED,
        expected_row_version=0,
        idempotency_key="status:p-w008-lifecycle-concurrency:pause",
        reason="W008 exact-parent concurrent replay proof",
        changed_by_ref="qa://eng09-w008",
    )
    assert replay == results[0]

    with SQLiteStore(db_path).connection() as conn:
        project = conn.execute(
            "SELECT status,row_version FROM product_projects WHERE project_id=?",
            ("p-w008-lifecycle-concurrency",),
        ).fetchone()
        assert project is not None
        assert project["status"] == ProductProjectState.PAUSED.value
        assert project["row_version"] == 1

        mutation_count = conn.execute(
            "SELECT COUNT(*) AS count FROM product_project_mutation_idempotency "
            "WHERE operation_key=?",
            ("status:p-w008-lifecycle-concurrency:pause",),
        ).fetchone()
        assert mutation_count is not None
        assert mutation_count["count"] == 1

        audit_count = conn.execute(
            "SELECT COUNT(*) AS count FROM audit_events "
            "WHERE event_type='product_project.status_changed' "
            "AND entity_type='product_project' AND entity_id=?",
            ("p-w008-lifecycle-concurrency",),
        ).fetchone()
        assert audit_count is not None
        assert audit_count["count"] == 1
