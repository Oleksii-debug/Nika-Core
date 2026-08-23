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


def test_workspace_registration_fails_closed_on_corrupt_persisted_source_identity(
    tmp_path: Path,
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repository = ResearchRepository(store)
    repository.upsert_workspace(ResearchWorkspace("ws-a", "Research A"))
    network = NetworkResearchRepository(store)
    network.register_source(
        SourceSpec("source-a", "ws-a", SourceKind.HTTP, "https://example.com/original")
    )

    with store.connection() as conn:
        conn.execute(
            "UPDATE research_http_sources SET url=? WHERE source_id=?",
            ("http://[broken", "source-a"),
        )

    restarted = NetworkResearchRepository(SQLiteStore(store.path))
    with pytest.raises(ResearchSourceIdentityError) as rejected:
        restarted.register_source(
            SourceSpec("source-b", "ws-a", SourceKind.HTTP, "https://example.org/new")
        )

    assert rejected.value.code == "source_identity_corrupt"
    with store.connection() as conn:
        created = conn.execute(
            "SELECT 1 FROM research_http_sources WHERE source_id='source-b'"
        ).fetchone()
    assert created is None
