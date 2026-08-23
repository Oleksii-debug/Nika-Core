from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

from nika_core.data.sqlite import SQLiteStore
from nika_core.memory import MemoryScope, MemoryService


class _PauseBeforeReturnMemoryService(MemoryService):
    def __init__(self, store: SQLiteStore, *, entered: Event, release: Event) -> None:
        super().__init__(store)
        self._entered = entered
        self._release = release

    def get(self, **kwargs):
        self._entered.set()
        assert self._release.wait(timeout=5), "writer A was not released"
        return super().get(**kwargs)


def test_put_returns_the_exact_value_committed_by_that_writer(tmp_path: Path) -> None:
    path = tmp_path / "nika.db"
    store = SQLiteStore(path)
    store.initialize()

    a_committed = Event()
    release_a = Event()
    writer_a = _PauseBeforeReturnMemoryService(
        SQLiteStore(path),
        entered=a_committed,
        release=release_a,
    )
    writer_b = MemoryService(SQLiteStore(path))

    def put_a():
        return writer_a.put(
            scope=MemoryScope.WORKSPACE,
            owner_id="workspace-a",
            namespace="shared",
            key="same-key",
            value={"writer": "a"},
        )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future_a = pool.submit(put_a)
        assert a_committed.wait(timeout=5), "writer A did not reach its post-commit return read"

        returned_b = writer_b.put(
            scope=MemoryScope.WORKSPACE,
            owner_id="workspace-a",
            namespace="shared",
            key="same-key",
            value={"writer": "b"},
        )
        assert returned_b.value == {"writer": "b"}

        release_a.set()
        returned_a = future_a.result(timeout=5)

    assert returned_a.value == {"writer": "a"}, (
        "writer A returned a later writer's value after its own commit; "
        f"returned={returned_a.value!r}"
    )
    current = MemoryService(SQLiteStore(path)).get(
        scope=MemoryScope.WORKSPACE,
        owner_id="workspace-a",
        namespace="shared",
        key="same-key",
    )
    assert current is not None
    assert current.value == {"writer": "b"}
