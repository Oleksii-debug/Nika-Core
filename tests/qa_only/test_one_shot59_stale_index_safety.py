from __future__ import annotations

import sqlite3
from pathlib import Path

from nika_core.data.sqlite import SQLiteStore
from nika_core.research import (
    DeterministicResearchQueryService,
    LocalCorpusService,
    NetworkResearchRepository,
    ResearchQuerySpec,
    ResearchRepository,
    ResearchSearchFilters,
    ResearchWorkspace,
    SourceKind,
    SourceSpec,
)


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    ResearchRepository(store).upsert_workspace(
        ResearchWorkspace(workspace_id="ws", name="Stale index QA")
    )
    return store


def _query(
    store: SQLiteStore,
    text: str,
    *,
    source_id: str | None = None,
):
    service = DeterministicResearchQueryService(
        store=store,
        network_repository=NetworkResearchRepository(store),
    )
    filters = (
        ResearchSearchFilters(source_ids=(source_id,))
        if source_id is not None
        else ResearchSearchFilters()
    )
    return service.execute(
        ResearchQuerySpec(workspace_id="ws", text=text, filters=filters)
    ).result_set.items


def test_local_source_reingest_cannot_feed_superseded_sensitive_content_after_restart(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    root = tmp_path / "sources"
    root.mkdir()
    source_path = root / "policy.txt"
    source = SourceSpec("local-policy", "ws", SourceKind.LOCAL_FILE, str(source_path))
    corpus = LocalCorpusService(ResearchRepository(store), allowed_root=root)

    source_path.write_text("obsolete-sensitive-canary-59", encoding="utf-8")
    old = corpus.ingest(source)
    source_path.write_text("current-safe-marker-59", encoding="utf-8")
    current = corpus.ingest(source)
    assert old.document.document_id != current.document.document_id

    before_restart = _query(store, "obsolete-sensitive-canary-59", source_id=source.source_id)

    restarted = SQLiteStore(store.path)
    restarted.initialize()
    after_restart = _query(
        restarted,
        "obsolete-sensitive-canary-59",
        source_id=source.source_id,
    )
    current_after_restart = _query(
        restarted,
        "current-safe-marker-59",
        source_id=source.source_id,
    )

    assert before_restart == ()
    assert after_restart == ()
    assert [item.document_id for item in current_after_restart] == [
        current.document.document_id
    ]


def test_orphan_fts_row_cannot_feed_deleted_document_after_restart(tmp_path: Path) -> None:
    store = _store(tmp_path)
    root = tmp_path / "sources"
    root.mkdir()
    source_path = root / "delete-me.txt"
    source_path.write_text("deleted-sensitive-canary-59", encoding="utf-8")
    source = SourceSpec("delete-source", "ws", SourceKind.LOCAL_FILE, str(source_path))
    created = LocalCorpusService(ResearchRepository(store), allowed_root=root).ingest(source)

    raw = sqlite3.connect(store.path)
    try:
        raw.execute("PRAGMA foreign_keys = OFF")
        raw.execute(
            "DELETE FROM corpus_documents WHERE document_id=?",
            (created.document.document_id,),
        )
        raw.commit()
        stale_rows = raw.execute(
            "SELECT document_id FROM corpus_fts WHERE corpus_fts MATCH ?",
            ('"deleted-sensitive-canary-59"',),
        ).fetchall()
    finally:
        raw.close()
    assert stale_rows == [(created.document.document_id,)]

    before_restart = _query(store, "deleted-sensitive-canary-59")
    restarted = SQLiteStore(store.path)
    restarted.initialize()
    after_restart = _query(restarted, "deleted-sensitive-canary-59")

    assert before_restart == ()
    assert after_restart == ()
