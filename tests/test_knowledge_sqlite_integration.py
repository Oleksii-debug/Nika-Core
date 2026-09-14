from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.research.knowledge import (
    KnowledgeCorpus,
    KnowledgeIngestRequest,
    RetrievalScope,
)
from nika_core.research.knowledge_schema import KNOWLEDGE_SCHEMA_VERSION

_TIMESTAMP = "2026-08-23T00:00:00+00:00"


def _seed_live_workspace_and_source(store: SQLiteStore) -> None:
    with store.connection() as conn:
        conn.execute(
            """INSERT INTO research_workspaces(workspace_id, name, created_at, updated_at)
            VALUES ('ws-live', 'Live', ?, ?)""",
            (_TIMESTAMP, _TIMESTAMP),
        )
        conn.execute(
            """INSERT INTO research_sources(
                source_id, workspace_id, kind, locator, created_at, updated_at
            ) VALUES ('source-live', 'ws-live', 'local_file', 'file:///live.txt', ?, ?)""",
            (_TIMESTAMP, _TIMESTAMP),
        )


def _live_request() -> KnowledgeIngestRequest:
    return KnowledgeIngestRequest(
        workspace_id="ws-live",
        artifact_key="source:live",
        title="Live guide",
        media_type="text/plain",
        text="durable sqlite retrieval marker",
        source_locator="file:///live.txt",
        parser_name="text",
        parser_version="1",
        approved_by="user:owner",
        source_id="source-live",
    )


def test_sqlite_store_initializes_independent_knowledge_schema(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    assert store.knowledge_schema_version() == KNOWLEDGE_SCHEMA_VERSION


def test_sqlite_store_knowledge_ingest_and_search_survive_restart(tmp_path: Path) -> None:
    path = tmp_path / "nika.db"
    store = SQLiteStore(path)
    store.initialize()
    _seed_live_workspace_and_source(store)
    KnowledgeCorpus(store).ingest(_live_request())

    restarted_store = SQLiteStore(path)
    restarted_store.initialize()
    restarted = KnowledgeCorpus(restarted_store)
    scope = RetrievalScope(principal_id="user:reader", workspace_ids=("ws-live",))
    hits = restarted.search(scope, "retrieval marker")
    assert len(hits) == 1
    assert hits[0].provenance.artifact_key == "source:live"
    assert hits[0].provenance.source_id == "source-live"
    assert restarted_store.knowledge_schema_version() == KNOWLEDGE_SCHEMA_VERSION


def test_sqlite_store_restart_rejects_missing_workspace_parent_corruption(
    tmp_path: Path,
) -> None:
    path = tmp_path / "nika.db"
    store = SQLiteStore(path)
    store.initialize()
    _seed_live_workspace_and_source(store)
    KnowledgeCorpus(store).ingest(_live_request())

    with sqlite3.connect(path) as raw:
        raw.execute("PRAGMA foreign_keys = OFF")
        raw.execute("DELETE FROM research_workspaces WHERE workspace_id='ws-live'")

    with pytest.raises(RuntimeError, match="knowledge schema foreign-key integrity check failed"):
        SQLiteStore(path).initialize()