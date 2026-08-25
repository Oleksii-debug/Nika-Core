from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.research import NetworkResearchRepository, ResearchRepository, ResearchWorkspace
from nika_core.research.models import SourceKind, SourceSpec
from nika_core.research.source_identity import ResearchSourceIdentityError


@pytest.mark.parametrize(
    "locator",
    [
        "https://example.com/data?access_token=super-secret",
        "https://example.com/data?X-Amz-Signature=super-secret",
        "https://example.com/data?AWSAccessKeyId=super-secret",
        "https://example.com/data?GoogleAccessId=super-secret",
        "https://example.com/data?key=super-secret",
        "https://example.com/data?auth=super-secret",
    ],
)
def test_query_credentials_are_rejected_before_source_persistence(
    tmp_path: Path,
    locator: str,
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repository = ResearchRepository(store)
    repository.upsert_workspace(ResearchWorkspace("ws", "Research"))
    network = NetworkResearchRepository(store)

    with pytest.raises(ResearchSourceIdentityError) as rejected:
        network.register_source(SourceSpec("secret", "ws", SourceKind.HTTP, locator))

    assert rejected.value.code == "credentials_forbidden"
    assert "super-secret" not in str(rejected.value)
    with store.connection() as conn:
        row = conn.execute(
            "SELECT url FROM research_http_sources WHERE source_id='secret'"
        ).fetchone()
    assert row is None
