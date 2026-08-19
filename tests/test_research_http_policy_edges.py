from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.research import (
    ContentAddressedBlobStore,
    FreshnessState,
    HttpResearchService,
    HttpxResearchFetcher,
    LocalCorpusService,
    NetworkResearchRepository,
    RefreshDisposition,
    ResearchRepository,
    ResearchWorkspace,
    SourceKind,
    SourceSpec,
)

PUBLIC_IP = "93.184.216.34"


def _resolver(host: str, port: int) -> tuple[str, ...]:
    del host, port
    return (PUBLIC_IP,)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com:not-a-port/x",
        "https://[broken/x",
        "ftp://example.com/file",
        "http://example.com/plain",
    ],
)
def test_malformed_or_disallowed_urls_are_classified_not_raised(url: str) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    fetcher = HttpxResearchFetcher(
        resolver=_resolver,
        transport=httpx.MockTransport(handler),
    )
    result = fetcher.fetch(url)

    assert result.disposition is RefreshDisposition.BLOCKED
    assert result.error_code == "network_policy"
    assert requests == []


def test_failure_after_prior_success_marks_source_stale(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repository = ResearchRepository(store)
    repository.upsert_workspace(ResearchWorkspace("ws", "Research"))
    network = NetworkResearchRepository(store)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        del request
        calls += 1
        if calls == 1:
            return httpx.Response(
                200,
                headers={"Content-Type": "text/plain"},
                content=b"durable source",
            )
        return httpx.Response(503)

    service = HttpResearchService(
        repository=repository,
        network_repository=network,
        blob_store=ContentAddressedBlobStore(tmp_path / "blobs"),
        fetcher=HttpxResearchFetcher(
            resolver=_resolver,
            transport=httpx.MockTransport(handler),
        ),
        sleeper=lambda _: None,
    )
    service.register_source(
        SourceSpec("web", "ws", SourceKind.HTTP, "https://example.com/page")
    )

    assert service.refresh_source("web").disposition is RefreshDisposition.CHANGED
    failure = service.refresh_source("web")

    assert failure.disposition is RefreshDisposition.FAILED
    assert failure.attempts == 3
    state = network.get_source("web")
    assert state.freshness is FreshnessState.STALE
    assert state.current_raw_sha256 is not None
    assert network.snapshot_count("web") == 1
    assert network.attempt_count("web") == 4


def test_http_source_id_cannot_alias_existing_local_source(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repository = ResearchRepository(store)
    repository.upsert_workspace(ResearchWorkspace("ws", "Research"))
    root = tmp_path / "sources"
    root.mkdir()
    path = root / "local.txt"
    path.write_text("local", encoding="utf-8")
    LocalCorpusService(repository, allowed_root=root).ingest(
        SourceSpec("shared-id", "ws", SourceKind.LOCAL_FILE, str(path))
    )

    network = NetworkResearchRepository(store)
    with pytest.raises(ValueError, match="already owned by a local source"):
        network.register_source(
            SourceSpec(
                "shared-id",
                "ws",
                SourceKind.HTTP,
                "https://example.com/page",
            )
        )