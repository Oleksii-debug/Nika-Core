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


def test_sqlite_store_initializes_independent_knowledge_schema(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    assert store.knowledge_schema_version() == KNOWLEDGE_SCHEMA_VERSION


def test_sqlite_store_knowledge_ingest_and_search_survive_restart(tmp_path: Path) -> None:
    path = tmp_path / "nika.db"
    store = SQLiteStore(path)
    store.initialize()
    with store.connection() as conn:
        conn.execute(
            """INSERT INTO research_workspaces(workspace_id, name, created_at, updated_at)
            VALUES ('ws-live', 'Live', '2026-08-23T00:00:00+00:00',
                    '2026-08-23T00:00:00+00:00')"""
        )
    corpus = KnowledgeCorpus(store)
    corpus.ingest(
        KnowledgeIngestRequest(
            workspace_id="ws-live",
            artifact_key="source:live",
            title="Live guide",
            media_type="text/plain",
            text="durable sqlite retrieval marker",
            source_locator="file:///live.txt",
            parser_name="text",
            parser_version="1",
            approved_by="user:owner",
        )
    )

    restarted_store = SQLiteStore(path)
    restarted_store.initialize()
    restarted = KnowledgeCorpus(restarted_store)
    scope = RetrievalScope(principal_id="user:reader", workspace_ids=("ws-live",))
    hits = restarted.search(scope, "retrieval marker")
    assert len(hits) == 1
    assert hits[0].provenance.artifact_key == "source:live"
    assert restarted_store.knowledge_schema_version() == KNOWLEDGE_SCHEMA_VERSION


def test_sqlite_store_restart_rejects_missing_workspace_parent_corruption(
    tmp_path: Path,
) -> None:
    path = tmp_path / "nika.db"
    store = SQLiteStore(path)
    store.initialize()
    with store.connection() as conn:
        conn.execute(
            """INSERT INTO research_workspaces(workspace_id, name, created_at, updated_at)
            VALUES ('ws-live', 'Live', '2026-08-23T00:00:00+00:00',
                    '2026-08-23T00:00:00+00:00')"""
        )
    KnowledgeCorpus(store).ingest(
        KnowledgeIngestRequest(
            workspace_id="ws-live",
            artifact_key="source:live",
            title="Live guide",
            media_type="text/plain",
            text="durable sqlite retrieval marker",
            source_locator="file:///live.txt",
            parser_name="text",
            parser_version="1",
            approved_by="user:owner",
        )
    )

    with sqlite3.connect(path) as raw:
        raw.execute("PRAGMA foreign_keys = OFF")
        raw.execute("DELETE FROM research_workspaces WHERE workspace_id='ws-live'")

    with pytest.raises(RuntimeError, match="knowledge schema foreign-key integrity check failed"):
        SQLiteStore(path).initialize()
