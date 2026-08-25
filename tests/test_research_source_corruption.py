from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.research import (
    NetworkResearchRepository,
    ResearchRepository,
    ResearchWorkspace,
    SourceKind,
    SourceSpec,
)
from nika_core.research.source_identity import ResearchSourceIdentityError


def _network(tmp_path: Path) -> tuple[SQLiteStore, NetworkResearchRepository]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repository = ResearchRepository(store)
    repository.upsert_workspace(ResearchWorkspace("ws-a", "Research A"))
    return store, NetworkResearchRepository(store)


def test_registration_fails_closed_when_existing_workspace_identity_is_corrupt(
    tmp_path: Path,
) -> None:
    store, network = _network(tmp_path)
    network.register_source(
        SourceSpec(
            "source-a",
            "ws-a",
            SourceKind.HTTP,
            "https://example.com/original",
        )
    )

    with store.connection() as conn:
        conn.execute(
            "UPDATE research_http_sources SET url=? WHERE source_id=?",
            ("http://[broken", "source-a"),
        )

    restarted = NetworkResearchRepository(SQLiteStore(store.path))
    with pytest.raises(ResearchSourceIdentityError) as rejected:
        restarted.register_source(
            SourceSpec(
                "source-b",
                "ws-a",
                SourceKind.HTTP,
                "https://example.org/new",
            )
        )

    assert rejected.value.code == "source_identity_corrupt"
    with store.connection() as conn:
        created = conn.execute(
            "SELECT 1 FROM research_http_sources WHERE source_id='source-b'"
        ).fetchone()
    assert created is None


def test_restart_reads_do_not_expose_or_use_corrupt_credential_locator(tmp_path: Path) -> None:
    store, network = _network(tmp_path)
    network.register_source(
        SourceSpec(
            "source-a",
            "ws-a",
            SourceKind.HTTP,
            "https://example.com/source",
        )
    )

    secret = "do-not-disclose"
    with store.connection() as conn:
        conn.execute(
            "UPDATE research_http_sources SET url=? WHERE source_id=?",
            (f"https://example.com/source?api_key={secret}", "source-a"),
        )

    restarted = NetworkResearchRepository(SQLiteStore(store.path))
    with pytest.raises(ResearchSourceIdentityError) as direct_read:
        restarted.get_source("source-a")
    assert direct_read.value.code == "source_identity_corrupt"
    assert secret not in str(direct_read.value)

    with pytest.raises(ResearchSourceIdentityError) as batch_read:
        restarted.list_sources("ws-a")
    assert batch_read.value.code == "source_identity_corrupt"
    assert secret not in str(batch_read.value)
