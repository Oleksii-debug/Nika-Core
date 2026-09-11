from __future__ import annotations

from contextlib import contextmanager

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_project import ProductProjectRepository, ProductProjectSpec


def test_create_reserves_writer_before_first_durable_authority_read(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    trace: list[tuple[str, bool]] = []
    original_connection = store.connection

    @contextmanager
    def traced_connection():
        with original_connection() as conn:
            conn.set_trace_callback(
                lambda statement: trace.append((statement, conn.in_transaction))
            )
            yield conn

    store.connection = traced_connection  # type: ignore[method-assign]
    repo = ProductProjectRepository(store)
    spec = ProductProjectSpec(
        goal="Serialize create authority",
        desired_outcome="No read-then-write race between concurrent creates",
    )
    created = repo.create(
        project_id="p1",
        name="Product authority",
        spec=spec,
        idempotency_key="create:p1",
    )

    begin_index = next(
        index for index, (statement, _) in enumerate(trace) if statement == "BEGIN IMMEDIATE"
    )
    authority_read_index = next(
        index
        for index, (statement, _) in enumerate(trace)
        if "FROM product_project_idempotency" in statement
    )

    assert begin_index < authority_read_index
    assert trace[authority_read_index][1] is True
    assert created.spec_version == 1
    assert created.row_version == 0

    restarted_store = SQLiteStore(store.path)
    restarted_store.initialize()
    replay = ProductProjectRepository(restarted_store).create(
        project_id="p1",
        name="Product authority",
        spec=spec,
        idempotency_key="create:p1",
    )
    assert replay == created
