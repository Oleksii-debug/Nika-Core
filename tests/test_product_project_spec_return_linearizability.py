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


def _create_project(store: SQLiteStore, project_id: str) -> object:
    return ProductProjectRepository(store).create(
        project_id=project_id,
        name="Spec return linearizability",
        spec=_spec("initial"),
        idempotency_key=f"create:{project_id}",
    )


def test_update_spec_returns_own_revision_under_post_commit_interleaving(
    tmp_path,
    monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    created = _create_project(store, "p-linearizable")

    original_get = ProductProjectRepository.get
    writer_a_reached_post_commit_probe = threading.Event()
    release_writer_a_probe = threading.Event()

    def gated_get(self, project_id):
        if threading.current_thread().name == "writer-a":
            writer_a_reached_post_commit_probe.set()
            assert release_writer_a_probe.wait(timeout=10)
        return original_get(self, project_id)

    monkeypatch.setattr(ProductProjectRepository, "get", gated_get)
    results = {}
    failures = []

    def writer_a() -> None:
        try:
            repo = ProductProjectRepository(SQLiteStore(store.path))
            results["a"] = repo.update_spec(
                "p-linearizable",
                _spec("writer-a"),
                expected_row_version=created.row_version,
                idempotency_key="spec:writer-a",
                change_reason="writer a",
            )
        except BaseException as exc:  # pragma: no cover - surfaced by assertion
            failures.append(exc)

    thread = threading.Thread(target=writer_a, name="writer-a")
    thread.start()
    assert writer_a_reached_post_commit_probe.wait(timeout=10)

    writer_b = ProductProjectRepository(SQLiteStore(store.path)).update_spec(
        "p-linearizable",
        _spec("writer-b"),
        expected_row_version=1,
        idempotency_key="spec:writer-b",
        change_reason="writer b",
    )
    release_writer_a_probe.set()
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert failures == []
    writer_a_result = results["a"]
    assert (writer_a_result.spec_version, writer_a_result.row_version) == (2, 1)
    assert writer_a_result.spec.hypothesis == "writer-a"
    assert (writer_b.spec_version, writer_b.row_version) == (3, 2)
    assert writer_b.spec.hypothesis == "writer-b"


def test_replay_after_later_spec_mutation_returns_original_revision(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    created = _create_project(store, "p-replay-result")
    repo = ProductProjectRepository(store)

    first = repo.update_spec(
        "p-replay-result",
        _spec("first"),
        expected_row_version=created.row_version,
        idempotency_key="spec:first",
        change_reason="first",
    )
    second = repo.update_spec(
        "p-replay-result",
        _spec("second"),
        expected_row_version=first.row_version,
        idempotency_key="spec:second",
        change_reason="second",
    )
    replay = ProductProjectRepository(SQLiteStore(store.path)).update_spec(
        "p-replay-result",
        _spec("first"),
        expected_row_version=created.row_version,
        idempotency_key="spec:first",
        change_reason="first",
    )

    assert (first.spec_version, first.row_version, first.spec.hypothesis) == (2, 1, "first")
    assert (second.spec_version, second.row_version, second.spec.hypothesis) == (3, 2, "second")
    assert (replay.spec_version, replay.row_version, replay.spec.hypothesis) == (2, 1, "first")
    assert repo.get("p-replay-result").spec.hypothesis == "second"
