from __future__ import annotations

from nika_core.data.sqlite import SQLiteStore
from nika_core.research.evidence_dedupe import ResearchEvidenceDeduplicator
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


class _DuplicatingEvidenceNetwork:
    def __init__(self, delegate: NetworkResearchRepository) -> None:
        self._delegate = delegate

    def evidence_for_document(self, document_id: str):
        evidence = self._delegate.evidence_for_document(document_id)
        return evidence + evidence

    def get_result_set(self, result_set_id: str):
        return self._delegate.get_result_set(result_set_id)


def _services(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repository = ResearchRepository(store)
    network = NetworkResearchRepository(store)
    repository.upsert_workspace(ResearchWorkspace(workspace_id="ws", name="Research"))
    return store, repository, network


def _ingest_local(
    repository: ResearchRepository,
    *,
    source_id: str,
    locator: str,
    text: str,
):
    source = SourceSpec(
        source_id=source_id,
        workspace_id="ws",
        kind=SourceKind.LOCAL_FILE,
        locator=locator,
    )
    repository.upsert_source(source)
    return repository.ingest_document(
        source,
        ExtractedDocument(title="Evidence", text=text, media_type="text/plain"),
    ).document


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


def test_same_source_same_content_does_not_multiply_result_or_evidence(tmp_path) -> None:
    store, repository, network = _services(tmp_path)
    document = _ingest_local(
        repository,
        source_id="local-a",
        locator="notes.txt",
        text="alpha evidence",
    )
    duplicate_network = _DuplicatingEvidenceNetwork(network)
    writer = ScopedResearchResultWriter(
        store=store,
        network_repository=duplicate_network,  # type: ignore[arg-type]
    )
    worse = SearchHit(
        document_id=document.document_id,
        title=document.title,
        snippet="alpha evidence",
        rank=-1.0,
    )
    better = SearchHit(
        document_id=document.document_id,
        title=document.title,
        snippet="alpha evidence",
        rank=-2.0,
    )

    result = writer.save(
        workspace_id="ws",
        query="alpha",
        hits=[worse, better],
        why_matched="fixture",
    )

    assert len(result.items) == 1
    assert result.items[0].rank == -2.0
    assert len(result.items[0].evidence) == 1
    assert result.items[0].evidence[0].source_id == "local-a"
    assert network.get_result_set(result.result_set_id) == result


def test_same_url_different_http_revision_remains_distinct(tmp_path) -> None:
    store, repository, network = _services(tmp_path)
    document = _ingest_local(
        repository,
        source_id="seed",
        locator="seed.txt",
        text="same extracted content",
    )
    locator = "https://example.test/report"
    network.register_source(
        SourceSpec(
            source_id="http-report",
            workspace_id="ws",
            kind=SourceKind.HTTP,
            locator=locator,
        )
    )
    first_snapshot = _attach_http_revision(
        store,
        network,
        source_id="http-report",
        locator=locator,
        document_id=document.document_id,
        raw_sha256="1" * 64,
        observed_at="2026-09-10T10:00:00+00:00",
    )
    second_snapshot = _attach_http_revision(
        store,
        network,
        source_id="http-report",
        locator=locator,
        document_id=document.document_id,
        raw_sha256="2" * 64,
        observed_at="2026-09-10T11:00:00+00:00",
    )
    evidence = tuple(
        item
        for item in network.evidence_for_document(document.document_id)
        if item.source_id == "http-report"
    )
    deduplicator = ResearchEvidenceDeduplicator(store)

    retained = deduplicator.deduplicate(document.document_id, evidence)
    identities = {
        deduplicator.identity(document.document_id, item).revision_id for item in retained
    }

    assert len(retained) == 2
    assert identities == {f"snapshot:{first_snapshot}", f"snapshot:{second_snapshot}"}


def test_different_sources_with_same_text_remain_independent_evidence(tmp_path) -> None:
    store, repository, network = _services(tmp_path)
    first = _ingest_local(
        repository,
        source_id="source-a",
        locator="a.txt",
        text="shared words",
    )
    second = _ingest_local(
        repository,
        source_id="source-b",
        locator="b.txt",
        text="shared words",
    )
    assert first.document_id == second.document_id
    deduplicator = ResearchEvidenceDeduplicator(store)

    retained = deduplicator.deduplicate(
        first.document_id,
        network.evidence_for_document(first.document_id),
    )

    assert {item.source_id for item in retained} == {"source-a", "source-b"}
    assert len(retained) == 2
