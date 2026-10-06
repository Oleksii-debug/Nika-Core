from __future__ import annotations

from contextlib import contextmanager

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_project import ProductProjectRepository, ProductProjectSpec
from nika_core.product_project_lifecycle import (
    ProductProjectLifecycleService,
    ProductProjectState,
)


def test_transition_reserves_writer_before_first_durable_authority_read(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    ProductProjectRepository(store).create(
        project_id="p1",
        name="Product authority",
        spec=ProductProjectSpec(
            goal="Serialize lifecycle authority",
            desired_outcome="No read-then-write race between concurrent status mutations",
        ),
        idempotency_key="create:p1",
    )

    trace: list[tuple[str, bool]] = []
    original_connection = store.connection

    @contextmanager
    def traced_connection():
        with original_connection() as conn:
            conn.set_trace_callback(lambda statement: trace.append((statement, conn.in_transaction)))
            yield conn

    store.connection = traced_connection  # type: ignore[method-assign]
    lifecycle = ProductProjectLifecycleService(store)
    transition = lifecycle.transition(
        "p1",
        ProductProjectState.PAUSED,
        expected_row_version=0,
        idempotency_key="status:p1:pause",
        reason="Verify serialized writer authority",
        changed_by_ref="agent://eng07",
    )

    begin_index = next(
        index for index, (statement, _) in enumerate(trace) if statement == "BEGIN IMMEDIATE"
    )
    authority_read_index = next(
        index
        for index, (statement, _) in enumerate(trace)
        if "FROM product_project_mutation_idempotency" in statement
    )

    assert begin_index < authority_read_index
    assert trace[authority_read_index][1] is True
    assert transition.row_version == 1

    restarted = ProductProjectLifecycleService(SQLiteStore(store.path))
    replay = restarted.transition(
        "p1",
        ProductProjectState.PAUSED,
        expected_row_version=0,
        idempotency_key="status:p1:pause",
        reason="Verify serialized writer authority",
        changed_by_ref="agent://eng07",
    )
    assert replay == transition
