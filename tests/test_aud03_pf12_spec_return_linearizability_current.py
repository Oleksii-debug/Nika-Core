from __future__ import annotations

import threading
from contextlib import contextmanager

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_project import ProductProjectRepository, ProductProjectSpec


def _spec(hypothesis: str) -> ProductProjectSpec:
    return ProductProjectSpec(
        goal="Preserve mutation return identity",
        desired_outcome="Each writer returns the revision it committed",
        hypothesis=hypothesis,
    )


class _PostCommitGateStore(SQLiteStore):
    def __init__(self, path, committed: threading.Event, release: threading.Event) -> None:
        super().__init__(path)
        self._committed = committed
        self._release = release

    @contextmanager
    def connection(self):
        with super().connection() as conn:
            yield conn
        if threading.current_thread().name == "writer-a":
            self._committed.set()
            assert self._release.wait(timeout=10)


def test_spec_update_returns_its_own_committed_revision_after_later_writer(tmp_path) -> None:
    path = tmp_path / "nika.db"
    store = SQLiteStore(path)
    store.initialize()
    created = ProductProjectRepository(store).create(
        project_id="p-aud03-linearizable-current",
        name="AUD03 current linearizable return",
        spec=_spec("initial"),
        idempotency_key="create:p-aud03-linearizable-current",
    )

    writer_a_committed = threading.Event()
    release_writer_a = threading.Event()
    results = {}
    failures: list[Exception] = []

    def writer_a() -> None:
        try:
            gated = _PostCommitGateStore(path, writer_a_committed, release_writer_a)
            results["a"] = ProductProjectRepository(gated).update_spec(
                created.project_id,
                _spec("writer-a"),
                expected_row_version=created.row_version,
                idempotency_key="spec:writer-a-current",
                change_reason="writer a",
            )
        except Exception as exc:  # pragma: no cover - surfaced by assertion
            failures.append(exc)

    thread = threading.Thread(target=writer_a, name="writer-a")
    thread.start()
    assert writer_a_committed.wait(timeout=10)

    writer_b = ProductProjectRepository(SQLiteStore(path)).update_spec(
        created.project_id,
        _spec("writer-b"),
        expected_row_version=1,
        idempotency_key="spec:writer-b-current",
        change_reason="writer b",
    )
    release_writer_a.set()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert failures == []
    writer_a_result = results["a"]
    assert (writer_a_result.spec_version, writer_a_result.row_version) == (2, 1)
    assert writer_a_result.spec.hypothesis == "writer-a"
    assert (writer_b.spec_version, writer_b.row_version) == (3, 2)
    assert writer_b.spec.hypothesis == "writer-b"
