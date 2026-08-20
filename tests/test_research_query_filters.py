from __future__ import annotations

from nika_core.data.sqlite import SQLiteStore
from nika_core.research.models import (
    ExtractedDocument,
    FreshnessState,
    RefreshDisposition,
    ResearchWorkspace,
    SourceKind,
    SourceSpec,
)
from nika_core.research.network_repository import NetworkResearchRepository
from nika_core.research.query import (
    DeterministicResearchQueryService,
    ResearchQuerySpec,
    ResearchSearchFilters,
    SearchMode,
)
from nika_core.research.repository import ResearchRepository


def _services(tmp_path):
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repository = ResearchRepository(store)
    network = NetworkResearchRepository(store)
    repository.upsert_workspace(ResearchWorkspace(workspace_id="ws", name="Research"))
    return store, repository, network


def _ingest(
    repository: ResearchRepository,
    *,
    source_id: str,
    locator: str,
    title: str,
    text: str,
    media_type: str = "text/plain",
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
        ExtractedDocument(title=title, text=text, media_type=media_type),
    ).document


def _attach_http_origin(
    store: SQLiteStore,
    network: NetworkResearchRepository,
    *,
    source_id: str,
    document_id: str,
    freshness: FreshnessState,
) -> None:
    source = SourceSpec(
        source_id=source_id,
        workspace_id="ws",
        kind=SourceKind.HTTP,
        locator=f"https://example.com/{source_id}",
    )
    network.register_source(source)
    artifact_id = f"artifact-{source_id}"
    raw_sha = (source_id * 64)[:64]
    with store.connection() as conn:
        conn.execute(
            """INSERT INTO corpus_artifacts(
                artifact_id, workspace_id, raw_sha256, byte_size, media_type,
                original_name, storage_relpath, created_at
            ) VALUES (?, 'ws', ?, 1, 'text/html', 'page.html', ?, '2026-08-20T00:00:00+00:00')""",
            (artifact_id, raw_sha, f"sha256/{source_id}"),
        )
    snapshot_id = network.record_snapshot(
        source_id=source_id,
        artifact_id=artifact_id,
        raw_sha256=raw_sha,
        media_type="text/html",
        etag=None,
        last_modified=None,
        extraction_id=None,
        document_id=document_id,
    )
    network.link_document_origin(
        document_id=document_id,
        source_id=source_id,
        snapshot_id=snapshot_id,
        locator=source.locator,
    )
    disposition = (
        RefreshDisposition.CHANGED
        if freshness is FreshnessState.CURRENT
        else RefreshDisposition.FAILED
    )
    network.finalize_source(
        source_id,
        disposition=disposition,
        final_url=source.locator,
        status_code=200 if freshness is FreshnessState.CURRENT else 503,
        current_raw_sha256=raw_sha if freshness is FreshnessState.CURRENT else None,
        error_code=None if freshness is FreshnessState.CURRENT else "fetch_failed",
        error_message="" if freshness is FreshnessState.CURRENT else "temporary failure",
    )
    if freshness is FreshnessState.STALE:
        with store.connection() as conn:
            conn.execute(
                "UPDATE research_http_sources SET freshness='stale' WHERE source_id=?",
                (source_id,),
            )


def test_phrase_mode_is_distinct_from_literal_token_mode(tmp_path) -> None:
    store, repository, network = _services(tmp_path)
    _ingest(
        repository,
        source_id="local-a",
        locator="a.txt",
        title="contiguous",
        text="alpha beta gamma",
    )
    _ingest(
        repository,
        source_id="local-b",
        locator="b.txt",
        title="separated",
        text="alpha middle beta gamma",
    )
    service = DeterministicResearchQueryService(store=store, network_repository=network)

    literal = service.execute(ResearchQuerySpec(workspace_id="ws", text="alpha beta"))
    phrase = service.execute(
        ResearchQuerySpec(workspace_id="ws", text="alpha beta", mode=SearchMode.PHRASE)
    )

    assert {item.title for item in literal.result_set.items} == {"contiguous", "separated"}
    assert [item.title for item in phrase.result_set.items] == ["contiguous"]


def test_source_kind_id_media_and_freshness_filters_share_origin_scope(tmp_path) -> None:
    store, repository, network = _services(tmp_path)
    current_doc = _ingest(
        repository,
        source_id="local-current",
        locator="current.txt",
        title="current grant",
        text="grant opportunity alpha",
        media_type="text/plain",
    )
    stale_doc = _ingest(
        repository,
        source_id="local-stale",
        locator="stale.csv",
        title="stale grant",
        text="grant opportunity beta",
        media_type="text/csv",
    )
    _attach_http_origin(
        store,
        network,
        source_id="http-current",
        document_id=current_doc.document_id,
        freshness=FreshnessState.CURRENT,
    )
    _attach_http_origin(
        store,
        network,
        source_id="http-stale",
        document_id=stale_doc.document_id,
        freshness=FreshnessState.STALE,
    )
    service = DeterministicResearchQueryService(store=store, network_repository=network)

    execution = service.execute(
        ResearchQuerySpec(
            workspace_id="ws",
            text="grant",
            filters=ResearchSearchFilters(
                source_ids=("http-current",),
                source_kinds=(SourceKind.HTTP,),
                media_types=("text/plain",),
                freshness=(FreshnessState.CURRENT,),
            ),
        )
    )

    assert [item.title for item in execution.result_set.items] == ["current grant"]
    assert execution.result_set.items[0].evidence
    assert any(
        evidence.source_id == "http-current"
        and evidence.freshness is FreshnessState.CURRENT
        for evidence in execution.result_set.items[0].evidence
    )

    wrong_source = service.execute(
        ResearchQuerySpec(
            workspace_id="ws",
            text="grant",
            filters=ResearchSearchFilters(
                source_ids=("http-stale",),
                freshness=(FreshnessState.CURRENT,),
            ),
        )
    )
    assert wrong_source.result_set.items == ()


def test_raw_fts_operators_are_not_exposed(tmp_path) -> None:
    store, repository, network = _services(tmp_path)
    _ingest(
        repository,
        source_id="local-a",
        locator="a.txt",
        title="alpha",
        text="alpha only",
    )
    _ingest(
        repository,
        source_id="local-b",
        locator="b.txt",
        title="beta",
        text="beta only",
    )
    service = DeterministicResearchQueryService(store=store, network_repository=network)

    execution = service.execute(
        ResearchQuerySpec(workspace_id="ws", text="alpha OR beta")
    )

    assert execution.result_set.items == ()


def test_result_set_survives_service_restart_and_text_report_is_explicit(tmp_path) -> None:
    store, repository, network = _services(tmp_path)
    _ingest(
        repository,
        source_id="local-a",
        locator="notes.txt",
        title="Ukrainian note",
        text="освіта грант можливість",
    )
    service = DeterministicResearchQueryService(store=store, network_repository=network)
    execution = service.execute(
        ResearchQuerySpec(
            workspace_id="ws",
            text="освіта грант",
            mode=SearchMode.PHRASE,
            filters=ResearchSearchFilters(source_ids=("local-a",)),
        )
    )

    reopened_store = SQLiteStore(store.path)
    reopened_store.initialize()
    reopened_network = NetworkResearchRepository(reopened_store)
    persisted = reopened_network.get_result_set(execution.result_set.result_set_id)
    rendered = service.render_text(execution)

    assert persisted == execution.result_set
    assert "Mode: phrase" in rendered
    assert "Source IDs: local-a" in rendered
    assert "local_file: notes.txt" in rendered


def test_filter_validation_fails_closed_for_unknown_or_cross_workspace_source(tmp_path) -> None:
    store, repository, network = _services(tmp_path)
    repository.upsert_workspace(ResearchWorkspace(workspace_id="other", name="Other"))
    other = SourceSpec(
        source_id="other-source",
        workspace_id="other",
        kind=SourceKind.LOCAL_FILE,
        locator="other.txt",
    )
    repository.upsert_source(other)
    service = DeterministicResearchQueryService(store=store, network_repository=network)

    for source_id, expected in (
        ("missing", "unknown source_ids"),
        ("other-source", "crosses workspace boundary"),
    ):
        try:
            service.execute(
                ResearchQuerySpec(
                    workspace_id="ws",
                    text="anything",
                    filters=ResearchSearchFilters(source_ids=(source_id,)),
                )
            )
        except ValueError as exc:
            assert expected in str(exc)
        else:
            raise AssertionError("invalid source filter must fail closed")
