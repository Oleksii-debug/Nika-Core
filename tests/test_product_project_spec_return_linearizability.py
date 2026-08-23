from __future__ import annotations

import threading
from contextlib import contextmanager

from nika_core.data.sqlite import SQLiteStore
from nika_core.product_project import (
    ProductProject,
    ProductProjectRepository,
    ProductProjectSpec,
)
from nika_core.product_project_lifecycle import (
    ProductProjectLifecycleService,
    ProductProjectState,
)


def _spec(hypothesis: str) -> ProductProjectSpec:
    return ProductProjectSpec(
        goal="Preserve mutation return identity",
        desired_outcome="Each writer returns the revision it committed",
        hypothesis=hypothesis,
    )


def _create_project(store: SQLiteStore, project_id: str) -> ProductProject:
    return ProductProjectRepository(store).create(
        project_id=project_id,
        name="Spec return linearizability",
        spec=_spec("initial"),
        idempotency_key=f"create:{project_id}",
    )


class _PostCommitGateStore(SQLiteStore):
    """Test-only gate after SQLite commit but before the caller's context exits."""

    def __init__(
        self,
        path,
        *,
        committed: threading.Event,
        release: threading.Event,
    ) -> None:
        super().__init__(path)
        self._committed = committed
        self._release = release
        self._armed = True

    @contextmanager
    def connection(self):
        with super().connection() as conn:
            yield conn
        if self._armed:
            self._armed = False
            self._committed.set()
            if not self._release.wait(timeout=10):
                raise RuntimeError("timed out waiting to release post-commit test gate")


def test_update_spec_returns_own_revision_under_post_commit_interleaving(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    created = _create_project(store, "p-linearizable")

    writer_a_committed = threading.Event()
    release_writer_a = threading.Event()
    gated_store = _PostCommitGateStore(
        store.path,
        committed=writer_a_committed,
        release=release_writer_a,
    )
    results: dict[str, ProductProject] = {}
    failures: list[Exception] = []

    def writer_a() -> None:
        try:
            results["a"] = ProductProjectRepository(gated_store).update_spec(
                "p-linearizable",
                _spec("writer-a"),
                expected_row_version=created.row_version,
                idempotency_key="spec:writer-a",
                change_reason="writer a",
            )
        except Exception as exc:  # pragma: no cover - surfaced by assertion
            failures.append(exc)

    thread = threading.Thread(target=writer_a, name="writer-a")
    thread.start()
    assert writer_a_committed.wait(timeout=10)

    try:
        writer_b = ProductProjectRepository(SQLiteStore(store.path)).update_spec(
            "p-linearizable",
            _spec("writer-b"),
            expected_row_version=1,
            idempotency_key="spec:writer-b",
            change_reason="writer b",
        )
    finally:
        release_writer_a.set()
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
    assert replay.updated_at == first.updated_at
    assert repo.get("p-replay-result").spec.hypothesis == "second"


def test_replay_preserves_status_at_original_spec_mutation_row_version(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    created = _create_project(store, "p-status-replay")
    repo = ProductProjectRepository(store)
    lifecycle = ProductProjectLifecycleService(store)

    paused = lifecycle.transition(
        "p-status-replay",
        ProductProjectState.PAUSED,
        expected_row_version=created.row_version,
        idempotency_key="status:pause",
        reason="pause before spec revision",
        changed_by_ref="test://owner",
    )
    mutation = repo.update_spec(
        "p-status-replay",
        _spec("while-paused"),
        expected_row_version=paused.row_version,
        idempotency_key="spec:while-paused",
        change_reason="revision while paused",
    )
    lifecycle.transition(
        "p-status-replay",
        ProductProjectState.ACTIVE,
        expected_row_version=mutation.row_version,
        idempotency_key="status:resume",
        reason="resume after spec revision",
        changed_by_ref="test://owner",
    )

    replay = ProductProjectRepository(SQLiteStore(store.path)).update_spec(
        "p-status-replay",
        _spec("while-paused"),
        expected_row_version=paused.row_version,
        idempotency_key="spec:while-paused",
        change_reason="revision while paused",
    )
    current = repo.get("p-status-replay")

    assert (mutation.spec_version, mutation.row_version, mutation.status) == (2, 2, "paused")
    assert (replay.spec_version, replay.row_version, replay.status) == (2, 2, "paused")
    assert replay.updated_at == mutation.updated_at
    assert (current.row_version, current.status) == (3, "active")
