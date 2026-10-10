from __future__ import annotations

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.research.models import (
    ExtractedDocument,
    ResearchWorkspace,
    SearchHit,
    SourceKind,
    SourceSpec,
)
from nika_core.research.network_repository import NetworkResearchRepository
from nika_core.research.query_results import ScopedResearchResultWriter
from nika_core.research.repository import ResearchRepository


def _attach_http_revision(
    store: SQLiteStore,
    network: NetworkResearchRepository,
    *,
    source_id: str,
    locator: str,
    document_id: str,
    raw_sha256: str,
    observed_at: str,
) -> str:
    artifact_id = f"artifact-{raw_sha256[:12]}"
    with store.connection() as conn:
        conn.execute(
            """INSERT INTO corpus_artifacts(
                artifact_id, workspace_id, raw_sha256, byte_size, media_type,
                original_name, storage_relpath, created_at
            ) VALUES (?, 'ws', ?, 1, 'text/html', 'page.html', ?, ?)""",
            (artifact_id, raw_sha256, f"sha256/{raw_sha256}", observed_at),
        )
    snapshot_id = network.record_snapshot(
        source_id=source_id,
        artifact_id=artifact_id,
        raw_sha256=raw_sha256,
        media_type="text/html",
        etag=None,
        last_modified=None,
        extraction_id=None,
        document_id=document_id,
    )
    with store.connection() as conn:
        conn.execute(
            """INSERT INTO corpus_http_origins(
                document_id, source_id, snapshot_id, locator, observed_at
            ) VALUES (?, ?, ?, ?, ?)""",
            (document_id, source_id, snapshot_id, locator, observed_at),
        )
    return snapshot_id


def test_ambiguous_http_revision_identity_fails_before_result_publication(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repository = ResearchRepository(store)
    network = NetworkResearchRepository(store)
    repository.upsert_workspace(ResearchWorkspace(workspace_id="ws", name="Research"))

    local_source = SourceSpec(
        source_id="seed",
        workspace_id="ws",
        kind=SourceKind.LOCAL_FILE,
        locator="seed.txt",
    )
    repository.upsert_source(local_source)
    document = repository.ingest_document(
        local_source,
        ExtractedDocument(
            title="Ambiguous evidence",
            text="same extracted content",
            media_type="text/plain",
        ),
    ).document

    locator = "https://example.test/report"
    network.register_source(
        SourceSpec(
            source_id="http-ambiguous",
            workspace_id="ws",
            kind=SourceKind.HTTP,
            locator=locator,
        )
    )
    observed_at = "2026-09-10T10:00:00+00:00"
    first_snapshot = _attach_http_revision(
        store,
        network,
        source_id="http-ambiguous",
        locator=locator,
        document_id=document.document_id,
        raw_sha256="1" * 64,
        observed_at=observed_at,
    )
    second_snapshot = _attach_http_revision(
        store,
        network,
        source_id="http-ambiguous",
        locator=locator,
        document_id=document.document_id,
        raw_sha256="2" * 64,
        observed_at=observed_at,
    )
    assert first_snapshot != second_snapshot

    writer = ScopedResearchResultWriter(store=store, network_repository=network)
    hit = SearchHit(
        document_id=document.document_id,
        title=document.title,
        snippet="same extracted content",
        rank=-1.0,
    )

    with pytest.raises(
        ValueError,
        match="ambiguous HTTP revision provenance cannot be deduplicated safely",
    ):
        writer.save(
            workspace_id="ws",
            query="same",
            hits=[hit],
            source_ids=("http-ambiguous",),
            why_matched="fixture",
        )

    with store.connection() as conn:
        result_sets = conn.execute("SELECT COUNT(*) FROM research_result_sets").fetchone()[0]
        result_items = conn.execute("SELECT COUNT(*) FROM research_result_items").fetchone()[0]
    assert result_sets == 0
    assert result_items == 0
