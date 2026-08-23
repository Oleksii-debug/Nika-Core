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
    ResearchFetchFailureClass,
    ResearchRepository,
    ResearchWorkspace,
    SourceKind,
    SourceSpec,
)


@pytest.mark.parametrize(
    ("status", "failure_class", "error_code"),
    [
        (401, ResearchFetchFailureClass.AUTH, "authentication_required"),
        (403, ResearchFetchFailureClass.AUTH, "authentication_required"),
        (415, ResearchFetchFailureClass.HTTP, "http_status"),
    ],
)
def test_refresh_result_preserves_typed_http_failure_handoff(
    tmp_path: Path,
    status: int,
    failure_class: ResearchFetchFailureClass,
    error_code: str,
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repository = ResearchRepository(store)
    repository.upsert_workspace(ResearchWorkspace("ws", "Research"))
    network = NetworkResearchRepository(store)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, request=request)

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
        SourceSpec("remote", "ws", SourceKind.HTTP, "https://example.com/source")
    )

    result = service.refresh_source("remote")

    assert result.disposition in {RefreshDisposition.BLOCKED, RefreshDisposition.FAILED}
    assert result.error_code == error_code
    assert result.failure_class is failure_class


def test_refresh_result_preserves_private_network_classification(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repository = ResearchRepository(store)
    repository.upsert_workspace(ResearchWorkspace("ws", "Research"))
    network = NetworkResearchRepository(store)
    service = HttpResearchService(
        repository=repository,
        network_repository=network,
        blob_store=ContentAddressedBlobStore(tmp_path / "blobs"),
        fetcher=HttpxResearchFetcher(
            resolver=lambda host, port: ("127.0.0.1",),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, request=request)
            ),
        ),
        sleeper=lambda _: None,
    )
    service.register_source(
        SourceSpec("private", "ws", SourceKind.HTTP, "https://example.com/private")
    )

    result = service.refresh_source("private")

    assert result.disposition is RefreshDisposition.BLOCKED
    assert result.error_code == "network_policy"
    assert result.failure_class is ResearchFetchFailureClass.PRIVATE
