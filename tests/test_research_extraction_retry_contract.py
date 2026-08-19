from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.research import (
    ContentAddressedBlobStore,
    HttpResearchService,
    HttpxResearchFetcher,
    NetworkResearchRepository,
    RefreshDisposition,
    ResearchRepository,
    ResearchWorkspace,
    SourceKind,
    SourceSpec,
)
from nika_core.research.local import LocalIngestionError
import nika_core.research.web_service as web_service


def test_failed_extraction_retry_reuses_fetched_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repository = ResearchRepository(store)
    repository.upsert_workspace(ResearchWorkspace("ws", "Research"))
    network = NetworkResearchRepository(store)

    fetch_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal fetch_calls
        fetch_calls += 1
        if fetch_calls > 1:
            raise AssertionError("extraction retry must not fetch the source again")
        return httpx.Response(
            200,
            headers={"Content-Type": "text/plain", "ETag": '"v1"'},
            content="повторне вилучення".encode(),
        )

    original_extract = web_service.extract_text_payload
    extraction_calls = 0

    def flaky_extract(*args, **kwargs):
        nonlocal extraction_calls
        extraction_calls += 1
        if extraction_calls == 1:
            raise LocalIngestionError("synthetic extractor failure")
        return original_extract(*args, **kwargs)

    monkeypatch.setattr(web_service, "extract_text_payload", flaky_extract)

    service = HttpResearchService(
        repository=repository,
        network_repository=network,
        blob_store=ContentAddressedBlobStore(tmp_path / "blobs"),
        fetcher=HttpxResearchFetcher(
            resolver=lambda host, port: ("93.184.216.34",),
            transport=httpx.MockTransport(handler),
        ),
        sleeper=lambda _: None,
    )
    service.register_source(
        SourceSpec(
            "retry-source",
            "ws",
            SourceKind.HTTP,
            "https://example.com/data.txt",
        )
    )

    first = service.refresh_source("retry-source")
    assert first.disposition is RefreshDisposition.FAILED
    assert first.error_code == "extraction_failed"
    assert first.snapshot_id is not None
    assert fetch_calls == 1
    assert extraction_calls == 1

    second = service.refresh_source("retry-source")

    assert second.disposition is RefreshDisposition.CHANGED
    assert second.document_id is not None
    assert fetch_calls == 1
    assert extraction_calls == 2
    assert repository.search("ws", "повторне")
