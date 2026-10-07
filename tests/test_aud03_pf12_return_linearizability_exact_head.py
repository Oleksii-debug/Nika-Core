from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_project import ProductProjectRepository, ProductProjectSpec


def _spec(hypothesis: str) -> ProductProjectSpec:
    return ProductProjectSpec(
        goal="Preserve mutation return identity",
        desired_outcome="Each writer returns the revision it committed",
        hypothesis=hypothesis,
    )


def test_committed_revision_survives_later_writer_before_public_return_and_restart_replay(
    tmp_path,
    monkeypatch,
) -> None:
    db_path = tmp_path / "nika.db"
    store = SQLiteStore(db_path)
    store.initialize()
    created = ProductProjectRepository(store).create(
        project_id="p-aud03-linearizable",
        name="AUD03 linearizable return",
        spec=_spec("initial"),
        idempotency_key="create:p-aud03-linearizable",
    )

    original_connection = SQLiteStore.connection
    writer_a_committed = threading.Event()
    release_writer_a_return = threading.Event()

    @contextmanager
    def gated_connection(self):
        with original_connection(self) as conn:
            yield conn
        if threading.current_thread().name.startswith("writer-a"):
            writer_a_committed.set()
            assert release_writer_a_return.wait(timeout=10)

    monkeypatch.setattr(SQLiteStore, "connection", gated_connection)

    def writer_a():
        return ProductProjectRepository(SQLiteStore(db_path)).update_spec(
            "p-aud03-linearizable",
            _spec("writer-a"),
            expected_row_version=created.row_version,
            idempotency_key="spec:writer-a",
            change_reason="writer a",
        )

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="writer-a") as executor:
        future_a = executor.submit(writer_a)
        assert writer_a_committed.wait(timeout=10)
        try:
            writer_b = ProductProjectRepository(SQLiteStore(db_path)).update_spec(
                "p-aud03-linearizable",
                _spec("writer-b"),
                expected_row_version=1,
                idempotency_key="spec:writer-b",
                change_reason="writer b",
            )
        finally:
            release_writer_a_return.set()
        writer_a_result = future_a.result(timeout=10)

    assert (writer_a_result.spec_version, writer_a_result.row_version) == (2, 1)
    assert writer_a_result.spec.hypothesis == "writer-a"
    assert (writer_b.spec_version, writer_b.row_version) == (3, 2)
    assert writer_b.spec.hypothesis == "writer-b"

    restarted = ProductProjectRepository(SQLiteStore(db_path))
    current = restarted.get("p-aud03-linearizable")
    assert (current.spec_version, current.row_version) == (3, 2)
    assert current.spec.hypothesis == "writer-b"

    replayed_a = restarted.update_spec(
        "p-aud03-linearizable",
        _spec("writer-a"),
        expected_row_version=created.row_version,
        idempotency_key="spec:writer-a",
        change_reason="writer a",
    )
    assert (replayed_a.spec_version, replayed_a.row_version) == (2, 1)
    assert replayed_a.spec.hypothesis == "writer-a"
