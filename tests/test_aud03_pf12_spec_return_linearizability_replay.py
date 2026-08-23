from __future__ import annotations

import threading

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_project import ProductProjectRepository, ProductProjectSpec


def _spec(hypothesis: str) -> ProductProjectSpec:
    return ProductProjectSpec(
        goal="Preserve mutation return identity",
        desired_outcome="Each writer returns the revision it committed",
        hypothesis=hypothesis,
    )


def test_update_spec_returns_its_own_committed_revision_under_interleaving(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    created = ProductProjectRepository(store).create(
        project_id="p-aud03-linearizable",
        name="AUD03 linearizable return",
        spec=_spec("initial"),
        idempotency_key="create:p-aud03-linearizable",
    )

    original_get = ProductProjectRepository.get
    writer_a_reached_post_commit_get = threading.Event()
    release_writer_a_get = threading.Event()

    def gated_get(self, project_id):
        if threading.current_thread().name == "writer-a":
            writer_a_reached_post_commit_get.set()
            assert release_writer_a_get.wait(timeout=10)
        return original_get(self, project_id)

    monkeypatch.setattr(ProductProjectRepository, "get", gated_get)
    results = {}
    failures = []

    def writer_a() -> None:
        try:
            repo = ProductProjectRepository(SQLiteStore(store.path))
            results["a"] = repo.update_spec(
                "p-aud03-linearizable",
                _spec("writer-a"),
                expected_row_version=created.row_version,
                idempotency_key="spec:writer-a",
                change_reason="writer a",
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            failures.append(exc)

    thread = threading.Thread(target=writer_a, name="writer-a")
    thread.start()
    assert writer_a_reached_post_commit_get.wait(timeout=10)

    writer_b = ProductProjectRepository(SQLiteStore(store.path)).update_spec(
        "p-aud03-linearizable",
        _spec("writer-b"),
        expected_row_version=1,
        idempotency_key="spec:writer-b",
        change_reason="writer b",
    )
    release_writer_a_get.set()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert failures == []
    writer_a_result = results["a"]
    assert (writer_a_result.spec_version, writer_a_result.row_version) == (2, 1)
    assert writer_a_result.spec.hypothesis == "writer-a"
    assert (writer_b.spec_version, writer_b.row_version) == (3, 2)
    assert writer_b.spec.hypothesis == "writer-b"
