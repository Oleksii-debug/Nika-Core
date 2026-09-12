from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

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


def test_scoped_result_writer_rejects_cross_workspace_document(tmp_path: Path) -> None:
    store, _network, writer, _document_a, document_b = _system(tmp_path)

    with pytest.raises(ResearchSourceIdentityError, match="result-set workspace"):
        writer.save(
            workspace_id="ws-a",
            query="cross workspace",
            hits=[_hit(document_b, "beta")],
            why_matched="workspace authority regression",
            result_set_id="mixed-result",
        )

    with store.connection() as conn:
        assert conn.execute(
            "SELECT 1 FROM research_result_sets WHERE result_set_id=?",
            ("mixed-result",),
        ).fetchone() is None


def test_network_result_writer_rejects_cross_workspace_document(tmp_path: Path) -> None:
    store, network, _writer, _document_a, document_b = _system(tmp_path)

    with pytest.raises(ResearchSourceIdentityError, match="result-set workspace"):
        network.save_result_set(
            workspace_id="ws-a",
            query="cross workspace",
            hits=[_hit(document_b, "beta")],
        )

    with store.connection() as conn:
        assert conn.execute(
            "SELECT 1 FROM research_result_sets WHERE workspace_id=? AND query=?",
            ("ws-a", "cross workspace"),
        ).fetchone() is None


def test_result_set_restart_rejects_cross_workspace_durable_item(tmp_path: Path) -> None:
    store, _network, writer, document_a, document_b = _system(tmp_path)
    result = writer.save(
        workspace_id="ws-a",
        query="alpha",
        hits=[_hit(document_a, "alpha")],
        why_matched="workspace authority regression",
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
    with pytest.raises(ResearchSourceIdentityError, match="result-set workspace"):
        restarted.get_result_set(result.result_set_id)


def test_result_set_same_workspace_survives_restart(tmp_path: Path) -> None:
    store, _network, writer, document_a, _document_b = _system(tmp_path)
    result = writer.save(
        workspace_id="ws-a",
        query="alpha",
        hits=[_hit(document_a, "alpha")],
        why_matched="workspace authority regression",
        result_set_id="valid-result",
    )

    replayed = NetworkResearchRepository(SQLiteStore(store.path)).get_result_set(
        result.result_set_id
    )

    assert replayed.workspace_id == "ws-a"
    assert [item.document_id for item in replayed.items] == [document_a]


def test_workspace_rejection_rolls_back_atomically(tmp_path: Path) -> None:
    store, _network, writer, document_a, document_b = _system(tmp_path)

    with pytest.raises((ResearchSourceIdentityError, sqlite3.IntegrityError)):
        writer.save(
            workspace_id="ws-a",
            query="mixed",
            hits=[_hit(document_a, "alpha"), _hit(document_b, "beta")],
            why_matched="workspace authority regression",
            result_set_id="atomic-result",
        )

    with store.connection() as conn:
        assert conn.execute(
            "SELECT 1 FROM research_result_sets WHERE result_set_id=?",
            ("atomic-result",),
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM research_result_items WHERE result_set_id=?",
            ("atomic-result",),
        ).fetchone() is None
