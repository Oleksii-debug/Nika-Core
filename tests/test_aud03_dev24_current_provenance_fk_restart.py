from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.research.knowledge import KnowledgeCorpus, KnowledgeIngestRequest


def _insert_workspace(store: SQLiteStore, workspace_id: str, name: str) -> None:
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO research_workspaces(workspace_id,name,created_at,updated_at) "
            "VALUES (?,?,?,?)",
            (
                workspace_id,
                name,
                "2026-08-24T00:00:00+00:00",
                "2026-08-24T00:00:00+00:00",
            ),
        )


def test_cross_workspace_durable_source_is_rejected_before_corpus_write(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "cross-workspace.db")
    store.initialize()
    _insert_workspace(store, "ws-a", "A")
    _insert_workspace(store, "ws-b", "B")
    with store.connection() as conn:
        conn.execute(
            """INSERT INTO research_sources(
                source_id, workspace_id, kind, locator, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                "source-b",
                "ws-b",
                "local_file",
                "file:///workspace-b/private.txt",
                "2026-08-24T00:00:00+00:00",
                "2026-08-24T00:00:00+00:00",
            ),
        )

    corpus = KnowledgeCorpus(store)
    with pytest.raises((ValueError, PermissionError)):
        corpus.ingest(
            KnowledgeIngestRequest(
                workspace_id="ws-a",
                artifact_key="artifact-a",
                title="Cross-workspace provenance fixture",
                media_type="text/plain",
                text="aud03 provenance marker",
                source_id="source-b",
                source_locator="file:///workspace-b/private.txt",
                parser_name="text",
                parser_version="1",
                approved_by="approval:aud03",
            )
        )

    assert corpus.version_numbers("ws-a", "artifact-a") == ()


def test_restart_rejects_knowledge_rows_with_missing_workspace_parent(tmp_path: Path) -> None:
    path = tmp_path / "orphaned-workspace.db"
    store = SQLiteStore(path)
    store.initialize()
    _insert_workspace(store, "ws-live", "Live")
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
            approved_by="approval:aud03",
        )
    )

    with sqlite3.connect(path) as raw:
        raw.execute("PRAGMA foreign_keys = OFF")
        raw.execute("DELETE FROM research_workspaces WHERE workspace_id='ws-live'")

    with pytest.raises(RuntimeError, match="knowledge schema foreign-key integrity check failed"):
        SQLiteStore(path).initialize()
