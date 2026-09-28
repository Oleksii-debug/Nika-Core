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
    ResearchResultService,
    ResearchWorkspace,
    SourceKind,
    SourceSpec,
)
from nika_core.research.http import ResearchFetchFailureClass
from nika_core.research.source_identity import ResearchSourceIdentityError

PUBLIC_IP = "93.184.216.34"


def _public_resolver(host: str, port: int) -> tuple[str, ...]:
    del host, port
    return (PUBLIC_IP,)


def _repositories(
    tmp_path: Path,
) -> tuple[SQLiteStore, ResearchRepository, NetworkResearchRepository]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repository = ResearchRepository(store)
    repository.upsert_workspace(ResearchWorkspace("ws-a", "Research A"))
    repository.upsert_workspace(ResearchWorkspace("ws-b", "Research B"))
    return store, repository, NetworkResearchRepository(store)


def test_http_source_identity_is_canonical_idempotent_and_deduplicated(tmp_path: Path) -> None:
    _, _, network = _repositories(tmp_path)

    created = network.register_source(
        SourceSpec(
            "source-a",
            "ws-a",
            SourceKind.HTTP,
            "HTTPS://Example.COM:443/path?b=2&a=1#section",
        )
    )
    repeated = network.register_source(
        SourceSpec(
            "source-a",
            "ws-a",
            SourceKind.HTTP,
            "https://example.com/path?b=2&a=1",
        )
    )

    assert created.url == "https://example.com/path?b=2&a=1"
    assert repeated.url == created.url

    with pytest.raises(ResearchSourceIdentityError) as duplicate:
        network.register_source(
            SourceSpec(
                "source-b",
                "ws-a",
                SourceKind.HTTP,
                "https://EXAMPLE.com:443/path?b=2&a=1#other",
            )
        )
    assert duplicate.value.code == "source_duplicate"
    assert network.list_sources("ws-a") == (repeated,)


def test_source_id_cannot_rebind_workspace_or_locator_after_restart(tmp_path: Path) -> None:
    store, _, network = _repositories(tmp_path)
    original = network.register_source(
        SourceSpec("source-a", "ws-a", SourceKind.HTTP, "https://example.com/original")
    )
    network.finalize_source(
        "source-a",
        disposition=RefreshDisposition.CHANGED,
        final_url=original.url,
        status_code=200,
        etag='"v1"',
        current_raw_sha256="a" * 64,
    )

    restarted = NetworkResearchRepository(SQLiteStore(store.path))
    with pytest.raises(ResearchSourceIdentityError) as workspace_conflict:
        restarted.register_source(
            SourceSpec("source-a", "ws-b", SourceKind.HTTP, original.url)
        )
    assert workspace_conflict.value.code == "source_workspace_conflict"

    with pytest.raises(ResearchSourceIdentityError) as locator_conflict:
        restarted.register_source(
            SourceSpec(
                "source-a",
                "ws-a",
                SourceKind.HTTP,
                "https://example.com/replacement",
            )
        )
    assert locator_conflict.value.code == "source_locator_conflict"

    preserved = restarted.get_source("source-a")
    assert preserved.workspace_id == "ws-a"
    assert preserved.url == original.url
    assert preserved.etag == '"v1"'
    assert preserved.current_raw_sha256 == "a" * 64


def test_credential_bearing_locator_is_rejected_before_sqlite_persistence(tmp_path: Path) -> None:
    store, _, network = _repositories(tmp_path)

    with pytest.raises(ResearchSourceIdentityError) as rejected:
        network.register_source(
            SourceSpec(
                "secret-source",
                "ws-a",
                SourceKind.HTTP,
                "https://user:super-secret@example.com/private",
            )
        )

    assert rejected.value.code == "credentials_forbidden"
    assert "super-secret" not in str(rejected.value)
    with store.connection() as conn:
        row = conn.execute(
            "SELECT url FROM research_http_sources WHERE source_id='secret-source'"
        ).fetchone()
    assert row is None


def test_fetch_failure_class_distinguishes_private_auth_unsupported_and_network() -> None:
    private = HttpxResearchFetcher(
        resolver=lambda host, port: ("127.0.0.1",),
        transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request)),
    ).fetch("https://example.com/private")
    assert private.disposition is RefreshDisposition.BLOCKED
    assert private.error_code == "network_policy"
    assert private.failure_class is ResearchFetchFailureClass.PRIVATE

    unsupported_source = HttpxResearchFetcher(
        resolver=_public_resolver,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, request=request)),
    ).fetch("ftp://example.com/archive")
    assert unsupported_source.disposition is RefreshDisposition.BLOCKED
    assert unsupported_source.error_code == "network_policy"
    assert unsupported_source.failure_class is ResearchFetchFailureClass.UNSUPPORTED

    auth = HttpxResearchFetcher(
        resolver=_public_resolver,
        transport=httpx.MockTransport(lambda request: httpx.Response(401, request=request)),
    ).fetch("https://example.com/auth")
    assert auth.disposition is RefreshDisposition.BLOCKED
    assert auth.error_code == "authentication_required"
    assert auth.failure_class is ResearchFetchFailureClass.AUTH

    unsupported_media = HttpxResearchFetcher(
        resolver=_public_resolver,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                request=request,
                headers={"Content-Type": "application/x-custom"},
                content=b"data",
            )
        ),
    ).fetch("https://example.com/custom")
    assert unsupported_media.disposition is RefreshDisposition.UNSUPPORTED
    assert unsupported_media.error_code == "unsupported_media_type"
    assert unsupported_media.failure_class is ResearchFetchFailureClass.UNSUPPORTED

    def fail_network(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    network = HttpxResearchFetcher(
        resolver=_public_resolver,
        transport=httpx.MockTransport(fail_network),
    ).fetch("https://example.com/offline")
    assert network.disposition is RefreshDisposition.FAILED
    assert network.retryable is True
    assert network.failure_class is ResearchFetchFailureClass.NETWORK


def test_evidence_handoff_keeps_original_locator_after_rebind_is_rejected(tmp_path: Path) -> None:
    store, repository, network = _repositories(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            headers={"Content-Type": "text/plain", "ETag": '"stable"'},
            content=b"stable research evidence",
        )

    service = HttpResearchService(
        repository=repository,
        network_repository=network,
        blob_store=ContentAddressedBlobStore(tmp_path / "blobs"),
        fetcher=HttpxResearchFetcher(
            resolver=_public_resolver,
            transport=httpx.MockTransport(handler),
        ),
        sleeper=lambda _: None,
    )
    service.register_source(
        SourceSpec(
            "source-a",
            "ws-a",
            SourceKind.HTTP,
            "https://example.com/source#ignored-fragment",
        )
    )
    refreshed = service.refresh_source("source-a")
    assert refreshed.disposition is RefreshDisposition.CHANGED

    results = ResearchResultService(repository=repository, network_repository=network)
    result_set = results.search("ws-a", "stable research evidence")

    with pytest.raises(ResearchSourceIdentityError):
        service.register_source(
            SourceSpec(
                "source-a",
                "ws-a",
                SourceKind.HTTP,
                "https://attacker.example/rebound",
            )
        )

    restarted = ResearchResultService(
        repository=ResearchRepository(SQLiteStore(store.path)),
        network_repository=NetworkResearchRepository(SQLiteStore(store.path)),
    ).get(result_set.result_set_id)
    assert len(restarted.items) == 1
    assert len(restarted.items[0].evidence) == 1
    evidence = restarted.items[0].evidence[0]
    assert evidence.source_id == "source-a"
    assert evidence.locator == "https://example.com/source"
    assert network.get_source("source-a").url == "https://example.com/source"
