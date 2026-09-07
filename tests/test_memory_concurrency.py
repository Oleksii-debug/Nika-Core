from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.memory import MemoryConflictError, MemoryScope, MemoryService


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    return store


def _identity() -> dict[str, object]:
    return {
        "scope": MemoryScope.WORKSPACE,
        "owner_id": "research",
        "namespace": "policy",
        "key": "ranking",
    }


def test_unconditional_put_remains_compatible_and_revision_is_monotonic(tmp_path: Path) -> None:
    memory = MemoryService(_store(tmp_path))
    first = memory.put(**_identity(), value={"mode": "stable"})
    second = memory.put(**_identity(), value={"mode": "new"})

    assert second.value == {"mode": "new"}
    assert second.created_at == first.created_at
    assert second.updated_at > first.updated_at


def test_compare_and_put_rejects_stale_revision_after_restart(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first_service = MemoryService(store)
    original = first_service.compare_and_put(
        **_identity(),
        value={"mode": "initial"},
        expected_updated_at=None,
    )

    restarted = MemoryService(store)
    observed = restarted.get(**_identity())
    assert observed == original

    winner = first_service.compare_and_put(
        **_identity(),
        value={"mode": "winner"},
        expected_updated_at=original.updated_at,
    )
    with pytest.raises(MemoryConflictError, match="revision changed"):
        restarted.compare_and_put(
            **_identity(),
            value={"mode": "stale"},
            expected_updated_at=observed.updated_at,
        )

    durable = MemoryService(store).get(**_identity())
    assert durable == winner
    assert durable is not None and durable.value == {"mode": "winner"}


def test_concurrent_create_binds_absent_identity_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    barrier = Barrier(2)

    def create(value: str) -> str:
        service = MemoryService(store)
        barrier.wait()
        try:
            service.compare_and_put(
                **_identity(),
                value={"writer": value},
                expected_updated_at=None,
            )
        except MemoryConflictError:
            return "conflict"
        return "created"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(create, ("a", "b")))

    assert sorted(results) == ["conflict", "created"]
    durable = MemoryService(store).get(**_identity())
    assert durable is not None
    assert durable.value in ({"writer": "a"}, {"writer": "b"})


def test_compare_and_delete_rejects_stale_writer(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first_service = MemoryService(store)
    original = first_service.put(**_identity(), value={"mode": "initial"})
    winner = first_service.compare_and_put(
        **_identity(),
        value={"mode": "winner"},
        expected_updated_at=original.updated_at,
    )

    restarted = MemoryService(store)
    with pytest.raises(MemoryConflictError, match="revision changed"):
        restarted.compare_and_delete(
            **_identity(),
            expected_updated_at=original.updated_at,
        )

    assert restarted.get(**_identity()) == winner
    assert restarted.compare_and_delete(
        **_identity(),
        expected_updated_at=winner.updated_at,
    )
    assert restarted.get(**_identity()) is None


def test_conditional_user_memory_still_requires_explicit_approval(tmp_path: Path) -> None:
    memory = MemoryService(_store(tmp_path))
    with pytest.raises(PermissionError):
        memory.compare_and_put(
            scope=MemoryScope.USER,
            owner_id="local-user",
            namespace="preferences",
            key="language",
            value="uk",
            expected_updated_at=None,
        )
