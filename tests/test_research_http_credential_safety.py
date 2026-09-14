from __future__ import annotations

from pathlib import Path

import httpx

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

PUBLIC_IP = "93.184.216.34"
OTHER_PUBLIC_IP = "1.1.1.1"


def _resolver(host: str, port: int) -> tuple[str, ...]:
    del port
    if host == "other.example":
        return (OTHER_PUBLIC_IP,)
    return (PUBLIC_IP,)


def test_direct_credential_urls_are_blocked_without_result_leakage() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, request=request)

    fetcher = HttpxResearchFetcher(
        resolver=_resolver,
        transport=httpx.MockTransport(handler),
    )
    secrets = ("userinfo-secret", "query-secret")
    urls = (
        f"https://user:{secrets[0]}@example.com/private",
        f"https://example.com/private?api_key={secrets[1]}",
    )

    for url, secret in zip(urls, secrets, strict=True):
        result = fetcher.fetch(url)
        assert result.disposition is RefreshDisposition.BLOCKED
        assert result.error_code == "network_policy"
        assert result.failure_class is ResearchFetchFailureClass.POLICY
        assert result.requested_url == "<redacted-http-url>"
        assert result.final_url == "<redacted-http-url>"
        assert secret not in repr(result)

    assert requests == []


def test_credential_bearing_redirect_is_blocked_before_second_request() -> None:
    requests: list[httpx.Request] = []
    secret = "redirect-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            302,
            request=request,
            headers={"Location": f"https://other.example/private?token={secret}"},
        )

    fetcher = HttpxResearchFetcher(
        resolver=_resolver,
        transport=httpx.MockTransport(handler),
    )
    result = fetcher.fetch("https://example.com/start")

    assert result.disposition is RefreshDisposition.BLOCKED
    assert result.error_code == "network_policy"
    assert result.failure_class is ResearchFetchFailureClass.POLICY
    assert result.requested_url == "https://example.com/start"
    assert result.final_url == "https://example.com/start"
    assert secret not in repr(result)
    assert len(requests) == 1
    assert requests[0].headers["host"] == "example.com"


def test_blocked_redirect_credentials_are_not_persisted_in_attempt_history(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repository = ResearchRepository(store)
    repository.upsert_workspace(ResearchWorkspace("ws", "Research"))
    network = NetworkResearchRepository(store)
    secret = "durable-secret"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            request=request,
            headers={"Location": f"https://other.example/private?signature={secret}"},
        )

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
        SourceSpec(
            "source",
            "ws",
            SourceKind.HTTP,
            "https://example.com/start",
        )
    )
    result = service.refresh_source("source")

    assert result.disposition is RefreshDisposition.BLOCKED
    assert result.failure_class is ResearchFetchFailureClass.POLICY
    with store.connection() as conn:
        row = conn.execute(
            """SELECT requested_url, final_url, error_message
            FROM research_http_attempts WHERE source_id=?""",
            ("source",),
        ).fetchone()
    assert row is not None
    durable_text = " ".join((row["requested_url"], row["final_url"], row["error_message"]))
    assert secret not in durable_text
    assert row["requested_url"] == "https://example.com/start"
    assert row["final_url"] == "https://example.com/start"
