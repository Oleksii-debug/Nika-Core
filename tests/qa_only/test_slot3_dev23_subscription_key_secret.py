from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.research import (
    HttpxResearchFetcher,
    NetworkResearchRepository,
    RefreshDisposition,
    ResearchRepository,
    ResearchSourceIdentityError,
    ResearchWorkspace,
    SourceKind,
    SourceSpec,
)
from nika_core.research.models import ResearchFetchFailureClass

_CANARY = "SLOT3_SYNTHETIC_SUBSCRIPTION_KEY_7f3c0d91"
_SECRET_LOCATOR = f"https://example.com/data?subscription-key={_CANARY}"


def _public_resolver(host: str, port: int) -> tuple[str, ...]:
    del host, port
    return ("93.184.216.34",)


def test_subscription_key_is_rejected_before_source_persistence(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    ResearchRepository(store).upsert_workspace(ResearchWorkspace("ws", "Research"))
    network = NetworkResearchRepository(store)

    with pytest.raises(ResearchSourceIdentityError) as rejected:
        network.register_source(
            SourceSpec("subscription-secret", "ws", SourceKind.HTTP, _SECRET_LOCATOR)
        )

    assert rejected.value.code == "credentials_forbidden"
    assert _CANARY not in str(rejected.value)

    with store.connection() as conn:
        row = conn.execute(
            "SELECT url FROM research_http_sources "
            "WHERE source_id='subscription-secret'"
        ).fetchone()
        serialized = "\n".join(
            str(value)
            for db_row in conn.execute("SELECT * FROM research_http_sources").fetchall()
            for value in db_row
            if value is not None
        )

    assert row is None
    assert _CANARY not in serialized


def test_subscription_key_fetch_is_blocked_before_transport() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, request=request, content=b"unexpected transport")

    result = HttpxResearchFetcher(
        resolver=_public_resolver,
        transport=httpx.MockTransport(handler),
    ).fetch(_SECRET_LOCATOR)

    assert result.disposition is RefreshDisposition.BLOCKED
    assert result.error_code == "network_policy"
    assert result.failure_class is ResearchFetchFailureClass.POLICY
    assert result.requested_url == "<redacted-http-url>"
    assert result.final_url == "<redacted-http-url>"
    assert _CANARY not in result.message
    assert requested == []


def test_benign_subscription_query_remains_supported(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    ResearchRepository(store).upsert_workspace(ResearchWorkspace("ws", "Research"))
    network = NetworkResearchRepository(store)
    locator = "https://example.com/data?subscription=public-catalog&view=road"

    created = network.register_source(
        SourceSpec("public-query", "ws", SourceKind.HTTP, locator)
    )

    assert created.url == locator
