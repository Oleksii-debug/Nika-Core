from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.research.chunking import ChunkPolicy
from nika_core.research.knowledge import (
    CorpusCorruptionError,
    KnowledgeCorpus,
    KnowledgeIngestRequest,
    RetrievalScope,
)
from nika_core.research.knowledge_schema import (
    KNOWLEDGE_SCHEMA_VERSION,
    initialize_knowledge_schema,
)

_TIMESTAMP = "2026-08-23T00:00:00+00:00"


def _make_store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    with store.connection() as conn:
        conn.execute(
            """INSERT INTO research_workspaces(workspace_id, name, created_at, updated_at)
            VALUES ('ws-a', 'A', ?, ?)""",
            (_TIMESTAMP, _TIMESTAMP),
        )
    return store


def _request(text: str) -> KnowledgeIngestRequest:
    return KnowledgeIngestRequest(
        workspace_id="ws-a",
        artifact_key="artifact-a",
        title="Knowledge fixture",
        media_type="text/plain",
        text=text,
        source_locator="approved:fixture-a",
        parser_name="text",
        parser_version="1",
        approved_by="approval:owner",
    )


def _historical_fts_rows(store: SQLiteStore) -> list[tuple[str, ...]]:
    with store.connection() as conn:
        rows = conn.execute(
            """SELECT c.workspace_id, c.artifact_key, c.version, c.ordinal,
                      c.chunk_id, c.text, v.title
            FROM knowledge_chunks AS c
            JOIN knowledge_versions AS v
              ON v.workspace_id=c.workspace_id
             AND v.artifact_key=c.artifact_key
             AND v.version=c.version
            WHERE c.workspace_id='ws-a' AND c.artifact_key='artifact-a'
              AND c.version=1
            ORDER BY c.ordinal"""
        ).fetchall()
    return [
        (
            row["workspace_id"],
            row["artifact_key"],
            str(row["version"]),
            str(row["ordinal"]),
            row["chunk_id"],
            row["title"],
            row["text"],
        )
        for row in rows
    ]


def _inject_historical_fts(store: SQLiteStore) -> None:
    rows = _historical_fts_rows(store)
    assert rows
    with store.connection() as conn:
        conn.executemany(
            """INSERT INTO knowledge_fts(
                workspace_id, artifact_key, version, ordinal, chunk_id, title, body
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )


def test_version_switch_keeps_history_but_indexes_only_current_chunks(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    corpus = KnowledgeCorpus(store)
    corpus.ingest(_request("obsolete ranking marker"))
    corpus.ingest(_request("current ranking marker"))

    with store.connection() as conn:
        versions = conn.execute(
            """SELECT version FROM knowledge_versions
            WHERE workspace_id='ws-a' AND artifact_key='artifact-a'
            ORDER BY version"""
        ).fetchall()
        chunk_versions = conn.execute(
            """SELECT DISTINCT version FROM knowledge_chunks
            WHERE workspace_id='ws-a' AND artifact_key='artifact-a'
            ORDER BY version"""
        ).fetchall()
        fts_versions = conn.execute(
            """SELECT DISTINCT CAST(version AS INTEGER) AS version FROM knowledge_fts
            WHERE workspace_id='ws-a' AND artifact_key='artifact-a'
            ORDER BY version"""
        ).fetchall()

    assert [int(row["version"]) for row in versions] == [1, 2]
    assert [int(row["version"]) for row in chunk_versions] == [1, 2]
    assert [int(row["version"]) for row in fts_versions] == [2]
    scope = RetrievalScope(principal_id="user:reader", workspace_ids=("ws-a",))
    assert corpus.search(scope, "obsolete") == []
    assert corpus.search(scope, "current")[0].provenance.version == 2
    assert corpus.verify_integrity().versions_checked == 2


def test_integrity_rejects_historical_version_fts_projection(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    corpus = KnowledgeCorpus(store)
    corpus.ingest(_request("obsolete history marker"))
    corpus.ingest(_request("current history marker"))
    _inject_historical_fts(store)

    with pytest.raises(CorpusCorruptionError, match="historical version"):
        corpus.verify_integrity()


def test_v3_migration_rebuilds_v2_fts_projection_without_losing_history(
    tmp_path: Path,
) -> None:
    store = _make_store(tmp_path)
    corpus = KnowledgeCorpus(store)
    corpus.ingest(_request("obsolete migration marker"))
    corpus.ingest(_request("current migration marker"))
    _inject_historical_fts(store)

    with store.connection() as conn:
        conn.execute("DELETE FROM knowledge_schema_migrations WHERE version=3")
        initialize_knowledge_schema(conn)
        versions = conn.execute(
            "SELECT version FROM knowledge_versions ORDER BY version"
        ).fetchall()
        chunk_versions = conn.execute(
            "SELECT DISTINCT version FROM knowledge_chunks ORDER BY version"
        ).fetchall()
        fts_versions = conn.execute(
            """SELECT DISTINCT CAST(version AS INTEGER) AS version
            FROM knowledge_fts ORDER BY version"""
        ).fetchall()
        schema_version = conn.execute(
            "SELECT MAX(version) FROM knowledge_schema_migrations"
        ).fetchone()[0]

    assert [int(row["version"]) for row in versions] == [1, 2]
    assert [int(row["version"]) for row in chunk_versions] == [1, 2]
    assert [int(row["version"]) for row in fts_versions] == [2]
    assert schema_version == KNOWLEDGE_SCHEMA_VERSION == 3
    assert corpus.verify_integrity().versions_checked == 2


def test_v3_rebuild_repairs_persistent_fts_without_contaminating_acl_bm25(
    tmp_path: Path,
) -> None:
    store = _make_store(tmp_path)
    corpus = KnowledgeCorpus(store)
    old_text = " ".join("rankingmarker historical" for _ in range(80))
    corpus.ingest(
        _request(old_text),
        chunk_policy=ChunkPolicy(max_chars=64, overlap_chars=8),
    )
    corpus.ingest(_request("rankingmarker current authority"))
    scope = RetrievalScope(principal_id="user:reader", workspace_ids=("ws-a",))
    baseline = corpus.search(scope, "rankingmarker")[0].rank

    _inject_historical_fts(store)
    with pytest.raises(CorpusCorruptionError, match="historical version"):
        corpus.verify_integrity()
    contaminated = corpus.search(scope, "rankingmarker")[0].rank
    assert contaminated == pytest.approx(baseline, rel=0.0, abs=1e-15)

    with store.connection() as conn:
        conn.execute("DELETE FROM knowledge_schema_migrations WHERE version=3")
        initialize_knowledge_schema(conn)
    repaired = corpus.search(scope, "rankingmarker")[0].rank
    assert repaired == pytest.approx(baseline, rel=0.0, abs=1e-15)
    assert corpus.verify_integrity().versions_checked == 2


def test_v3_migration_fails_closed_on_missing_current_version(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    corpus = KnowledgeCorpus(store)
    corpus.ingest(_request("migration corruption marker"))

    with store.connection() as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            """UPDATE knowledge_artifacts SET current_version=999
            WHERE workspace_id='ws-a' AND artifact_key='artifact-a'"""
        )
        conn.execute("DELETE FROM knowledge_schema_migrations WHERE version=3")

    with pytest.raises(RuntimeError, match="missing current version"):
        store.initialize()
    assert store.knowledge_schema_version() == 2