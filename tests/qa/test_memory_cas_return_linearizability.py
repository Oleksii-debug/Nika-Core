from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

from nika_core.data.sqlite import SQLiteStore
from nika_core.memory import MemoryScope, MemoryService


def _identity() -> dict[str, object]:
    return {
        "scope": MemoryScope.WORKSPACE,
        "owner_id": "qa-memory",
        "namespace": "linearizable-return",
        "key": "shared",
    }


class _DelayedReturnMemoryService(MemoryService):
    """Expose the current post-commit/pre-return read window deterministically."""

    def __init__(
        self,
        store: SQLiteStore,
        *,
        committed: Event,
        allow_return_read: Event,
    ) -> None:
        super().__init__(store)
        self._committed = committed
        self._allow_return_read = allow_return_read

    def get(self, **kwargs):  # type: ignore[no-untyped-def]
        # Current _put() calls get() only after the write transaction has closed.
        # Signal that another writer may now legally commit before this read occurs.
        self._committed.set()
        if not self._allow_return_read.wait(timeout=5):
            raise AssertionError("second writer did not complete inside return-read window")
        return super().get(**kwargs)


def test_successful_compare_and_put_returns_the_exact_revision_it_committed(
    tmp_path: Path,
) -> None:
    """A successful CAS must not return a later writer's value/revision.

    Exact production parent #682 currently commits A and then calls self.get() in a
    separate transaction. This oracle forces B into that window. Final durable state
    may be B, but A's API result must remain the exact A revision it committed.
    """

    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    baseline_service = MemoryService(store)
    baseline = baseline_service.compare_and_put(
        **_identity(),
        value={"writer": "baseline"},
        expected_updated_at=None,
    )

    a_committed = Event()
    b_completed = Event()
    writer_a = _DelayedReturnMemoryService(
        store,
        committed=a_committed,
        allow_return_read=b_completed,
    )

    def commit_a():
        return writer_a.compare_and_put(
            **_identity(),
            value={"writer": "A"},
            expected_updated_at=baseline.updated_at,
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future_a = pool.submit(commit_a)
        assert a_committed.wait(timeout=5), "writer A did not enter post-commit return window"

        writer_b = MemoryService(store)
        observed_a = writer_b.get(**_identity())
        assert observed_a is not None
        assert observed_a.value == {"writer": "A"}

        durable_b = writer_b.compare_and_put(
            **_identity(),
            value={"writer": "B"},
            expected_updated_at=observed_a.updated_at,
        )
        b_completed.set()
        returned_a = future_a.result(timeout=5)

    # Linearizable CAS return contract: A reports A/r1 even though B/r2 is now durable.
    assert returned_a.value == {"writer": "A"}
    assert returned_a.updated_at == observed_a.updated_at
    assert durable_b.value == {"writer": "B"}
    assert durable_b.updated_at > observed_a.updated_at

    restarted = MemoryService(SQLiteStore(store.path))
    final = restarted.get(**_identity())
    assert final == durable_b
