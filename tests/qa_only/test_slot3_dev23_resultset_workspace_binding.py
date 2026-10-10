from __future__ import annotations

import sqlite3
from pathlib import Path

from nika_core.data.sqlite import SQLiteStore
from nika_core.research import (
    LocalCorpusService,
    NetworkResearchRepository,
    ResearchRepository,
    ResearchSourceIdentityError,
    ResearchWorkspace,
    SearchHit,
    SourceKind,
    SourceSpec,
)
from nika_core.research.query_results import ScopedResearchResultWriter


def _system(
    tmp_path: Path,
) -> tuple[
    SQLiteStore,
    NetworkResearchRepository,
    ScopedResearchResultWriter,
    str,
    str,
]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    corpus = ResearchRepository(store)
    corpus.upsert_workspace(ResearchWorkspace(workspace_id="ws-a", name="Workspace A"))
    corpus.upsert_workspace(ResearchWorkspace(workspace_id="ws-b", name="Workspace B"))

    root = tmp_path / "sources"
    root.mkdir()
    path_a = root / "alpha.txt"
    path_b = root / "beta.txt"
    path_a.write_text("alpha evidence", encoding="utf-8")
    path_b.write_text("beta evidence", encoding="utf-8")
    local = LocalCorpusService(corpus, allowed_root=root)
    document_a = local.ingest(
        SourceSpec("source-a", "ws-a", SourceKind.LOCAL_FILE, str(path_a))
    ).document
    document_b = local.ingest(
        SourceSpec("source-b", "ws-b", SourceKind.LOCAL_FILE, str(path_b))
    ).document

    network = NetworkResearchRepository(store)
    writer = ScopedResearchResultWriter(store=store, network_repository=network)
    return store, network, writer, document_a.document_id, document_b.document_id


def _hit(document_id: str, label: str) -> SearchHit:
    return SearchHit(
        document_id=document_id,
        title=label,
        snippet=f"{label} evidence",
        rank=1.0,
    )


def test_result_writer_rejects_cross_workspace_document_substitution(tmp_path: Path) -> None:
    store, _network, writer, _document_a, document_b = _system(tmp_path)

    try:
        writer.save(
            workspace_id="ws-a",
            query="cross workspace",
            hits=[_hit(document_b, "beta")],
            why_matched="synthetic qa",
            result_set_id="mixed-result",
        )
    except (ResearchSourceIdentityError, ValueError, RuntimeError, sqlite3.IntegrityError):
        pass

    with store.connection() as conn:
        mixed = conn.execute(
            """SELECT 1
            FROM research_result_sets AS result_set
            JOIN research_result_items AS item
              ON item.result_set_id = result_set.result_set_id
            JOIN corpus_documents AS document
              ON document.document_id = item.document_id
            WHERE result_set.result_set_id = ?
              AND result_set.workspace_id <> document.workspace_id""",
            ("mixed-result",),
        ).fetchone()
    assert mixed is None


def test_result_set_restart_rejects_cross_workspace_durable_item(tmp_path: Path) -> None:
    store, _network, writer, document_a, document_b = _system(tmp_path)
    result = writer.save(
        workspace_id="ws-a",
        query="alpha",
        hits=[_hit(document_a, "alpha")],
        why_matched="synthetic qa",
        result_set_id="restart-result",
    )

    with store.connection() as conn:
        conn.execute(
            """UPDATE research_result_items
            SET document_id = ?
            WHERE result_set_id = ? AND ordinal = 0""",
            (document_b, result.result_set_id),
        )

    restarted = NetworkResearchRepository(SQLiteStore(store.path))
    try:
        replayed = restarted.get_result_set(result.result_set_id)
    except (ResearchSourceIdentityError, ValueError, RuntimeError, sqlite3.IntegrityError):
        return

    assert all(item.document_id != document_b for item in replayed.items)


def test_result_set_same_workspace_positive_control_survives_restart(tmp_path: Path) -> None:
    store, _network, writer, document_a, _document_b = _system(tmp_path)
    result = writer.save(
        workspace_id="ws-a",
        query="alpha",
        hits=[_hit(document_a, "alpha")],
        why_matched="synthetic qa",
        result_set_id="valid-result",
    )

    replayed = NetworkResearchRepository(SQLiteStore(store.path)).get_result_set(
        result.result_set_id
    )

    assert replayed.workspace_id == "ws-a"
    assert [item.document_id for item in replayed.items] == [document_a]
