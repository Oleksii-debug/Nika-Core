from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from threading import Event, local
from concurrent.futures import ThreadPoolExecutor

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.memory import MemoryConflictError, MemoryScope, MemoryService
import nika_core.memory.service as memory_service_module


def _identity() -> dict[str, object]:
    return {
        "scope": MemoryScope.WORKSPACE,
        "owner_id": "qa-memory",
        "namespace": "revision-aba",
        "key": "shared",
    }


class _FrozenDateTime(datetime):
    fixed = datetime(2026, 9, 7, 12, 0, 0, tzinfo=UTC)

    @classmethod
    def now(cls, tz=None):  # type: ignore[no-untyped-def]
        if tz is None:
            return cls.fixed.replace(tzinfo=None)
        return cls.fixed.astimezone(tz)


def test_unconditional_writer_cannot_reuse_a_cas_revision_after_stale_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every mutation that assigns updated_at must serialize on the same write boundary.

    The production parent starts BEGIN IMMEDIATE only for conditional writes. This
    oracle freezes the revision clock and pauses a legacy put() after it has selected
    R0. A CAS then commits B/R1. The stale legacy writer must not later overwrite
    with U while reusing R1, otherwise a caller holding B/R1 cannot detect the write.
    """

    monkeypatch.setattr(memory_service_module, "datetime", _FrozenDateTime)

    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    service = MemoryService(store)
    baseline = service.compare_and_put(
        **_identity(),
        value={"writer": "baseline"},
        expected_updated_at=None,
    )

    stale_read = Event()
    allow_unconditional = Event()
    thread_state = local()
    original_next_revision = memory_service_module._next_revision

    def gated_next_revision(existing: str | None):
        if getattr(thread_state, "writer", None) == "unconditional":
            stale_read.set()
            if not allow_unconditional.wait(timeout=5):
                raise AssertionError("CAS writer did not complete after stale read")
        return original_next_revision(existing)

    monkeypatch.setattr(memory_service_module, "_next_revision", gated_next_revision)

    def legacy_put():
        thread_state.writer = "unconditional"
        try:
            return MemoryService(store).put(
                **_identity(),
                value={"writer": "unconditional"},
            )
        finally:
            thread_state.writer = None

    with ThreadPoolExecutor(max_workers=1) as pool:
        future_unconditional = pool.submit(legacy_put)
        assert stale_read.wait(timeout=5), "legacy writer did not reach stale revision window"

        cas_writer = MemoryService(store)
        winner = cas_writer.compare_and_put(
            **_identity(),
            value={"writer": "cas"},
            expected_updated_at=baseline.updated_at,
        )
        assert winner.value == {"writer": "cas"}

        allow_unconditional.set()
        legacy_result = future_unconditional.result(timeout=5)

    final = MemoryService(store).get(**_identity())
    assert final is not None
    assert final.value == {"writer": "unconditional"}

    # A different committed value must have a different authority-bearing revision.
    assert final.updated_at > winner.updated_at
    assert legacy_result.updated_at == final.updated_at

    # A caller that observed B/R1 must now be stale. Reusing R1 is an ABA bug.
    with pytest.raises(MemoryConflictError, match="revision changed"):
        MemoryService(store).compare_and_put(
            **_identity(),
            value={"writer": "stale-caller"},
            expected_updated_at=winner.updated_at,
        )
