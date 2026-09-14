from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.research import (
    ContentAddressedBlobStore,
    FreshnessState,
    HttpResearchService,
    HttpxResearchFetcher,
    NetworkResearchRepository,
    ResearchRepository,
    ResearchResultService,
    ResearchWorkspace,
    SourceKind,
    SourceSpec,
)

PUBLIC_IP = "93.184.216.34"
FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "research_source_freshness_versions.json"


@pytest.fixture()
def source_versions() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _resolver(host: str, port: int) -> tuple[str, ...]:
    del host, port
    return (PUBLIC_IP,)


def _services(
    tmp_path: Path,
    *,
    handler: Callable[[httpx.Request], httpx.Response],
) -> tuple[
    SQLiteStore,
    ResearchRepository,
    NetworkResearchRepository,
    HttpResearchService,
]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repository = ResearchRepository(store)
    repository.upsert_workspace(ResearchWorkspace("ws", "Freshness QA"))
    network = NetworkResearchRepository(store)
    web = HttpResearchService(
        repository=repository,
        network_repository=network,
        blob_store=ContentAddressedBlobStore(tmp_path / "blobs"),
        fetcher=HttpxResearchFetcher(
            resolver=_resolver,
            transport=httpx.MockTransport(handler),
        ),
        sleeper=lambda _: None,
    )
    return store, repository, network, web


def test_historical_snapshot_cannot_borrow_current_freshness(
    tmp_path: Path,
    source_versions: dict[str, object],
) -> None:
    versions = source_versions["versions"]
    assert isinstance(versions, list)
    responses = iter(versions)

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        version = next(responses)
        assert isinstance(version, dict)
        return httpx.Response(
            200,
            headers={
                "Content-Type": "text/plain",
                "ETag": str(version["etag"]),
                "Last-Modified": str(version["last_modified"]),
            },
            content=str(version["body"]).encode(),
        )

    store, _, network, web = _services(tmp_path, handler=handler)
    source_id = str(source_versions["source_id"])
    source_url = str(source_versions["url"])
    web.register_source(SourceSpec(source_id, "ws", SourceKind.HTTP, source_url))

    first = web.refresh_source(source_id)
    second = web.refresh_source(source_id)

    assert first.document_id is not None
    assert second.document_id is not None
    assert first.document_id != second.document_id

    with store.connection() as conn:
        snapshots = conn.execute(
            """SELECT raw_sha256, etag, last_modified, document_id
            FROM research_http_snapshots
            WHERE source_id=? ORDER BY observed_at, snapshot_id""",
            (source_id,),
        ).fetchall()
    assert len(snapshots) == 2
    assert [row["etag"] for row in snapshots] == [
        str(versions[0]["etag"]),
        str(versions[1]["etag"]),
    ]
    assert [row["last_modified"] for row in snapshots] == [
        str(versions[0]["last_modified"]),
        str(versions[1]["last_modified"]),
    ]
    assert snapshots[0]["raw_sha256"] != snapshots[1]["raw_sha256"]

    old_evidence = network.evidence_for_document(first.document_id)
    new_evidence = network.evidence_for_document(second.document_id)

    assert len(old_evidence) == 1
    assert len(new_evidence) == 1
    assert old_evidence[0].freshness is FreshnessState.STALE
    assert new_evidence[0].freshness is FreshnessState.CURRENT


def test_undated_cached_result_never_claims_current_after_restart(
    tmp_path: Path,
    source_versions: dict[str, object],
) -> None:
    undated = source_versions["undated"]
    assert isinstance(undated, dict)

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            headers={"Content-Type": "text/plain"},
            content=str(undated["body"]).encode(),
        )

    store, _, network, web = _services(tmp_path, handler=handler)
    source_id = str(source_versions["source_id"])
    source_url = str(source_versions["url"])
    web.register_source(SourceSpec(source_id, "ws", SourceKind.HTTP, source_url))
    refreshed = web.refresh_source(source_id)
    assert refreshed.document_id is not None

    source = network.get_source(source_id)
    assert source.etag is None
    assert source.last_modified is None
    assert source.freshness is FreshnessState.CURRENT

    restarted_store = SQLiteStore(store.path)
    restarted_results = ResearchResultService(
        repository=ResearchRepository(restarted_store),
        network_repository=NetworkResearchRepository(restarted_store),
    )
    result_set = restarted_results.search("ws", "undated-cache-only")
    assert len(result_set.items) == 1
    assert len(result_set.items[0].evidence) == 1

    cached = result_set.items[0].evidence[0]
    rendered = restarted_results.render_text(result_set)

    assert cached.source_kind is SourceKind.HTTP
    assert cached.freshness is FreshnessState.UNKNOWN
    assert "cached" in rendered.lower()
    assert "freshness=unknown" in rendered
    assert "freshness=current" not in rendered
