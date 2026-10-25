from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from datetime import UTC, datetime
from pathlib import Path
from threading import Event

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.research import ResearchRepository, ResearchWorkspace, SourceKind, SourceSpec


def _count(store: SQLiteStore, table: str) -> int:
    with store.connection() as conn:
        row = conn.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()
    return int(row["count"])


def _hold_http_writer(
    store: SQLiteStore,
    *,
    writer_ready: Event,
    release_writer: Event,
) -> None:
    now = datetime.now(UTC).isoformat()
    with store.connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """INSERT INTO research_http_sources(
                source_id, workspace_id, url, freshness, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                "shared",
                "ws",
                "https://example.invalid/source",
                "unknown",
                now,
                now,
            ),
        )
        writer_ready.set()
        if not release_writer.wait(timeout=5):
            raise TimeoutError("test HTTP writer was not released")


def test_local_source_identity_serializes_after_http_writer(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repository = ResearchRepository(store)
    repository.upsert_workspace(ResearchWorkspace(workspace_id="ws", name="Research"))
    local_source = SourceSpec(
        "shared",
        "ws",
        SourceKind.LOCAL_FILE,
        str((tmp_path / "local.txt").resolve()),
    )
    writer_ready = Event()
    release_writer = Event()
    local_started = Event()

    def register_local() -> None:
        local_started.set()
        repository.upsert_source(local_source)

    with ThreadPoolExecutor(max_workers=2) as executor:
        http_future = executor.submit(
            _hold_http_writer,
            SQLiteStore(store.path),
            writer_ready=writer_ready,
            release_writer=release_writer,
        )
        assert writer_ready.wait(timeout=2)
        local_future = executor.submit(register_local)
        assert local_started.wait(timeout=2)
        try:
            with pytest.raises(FutureTimeoutError):
                local_future.result(timeout=0.1)
        finally:
            release_writer.set()

        http_future.result(timeout=2)
        with pytest.raises(ValueError, match="already owned by an HTTP source"):
            local_future.result(timeout=2)

    assert _count(store, "research_http_sources") == 1
    assert _count(store, "research_sources") == 0
    assert _count(store, "corpus_documents") == 0
    assert _count(store, "corpus_origins") == 0
