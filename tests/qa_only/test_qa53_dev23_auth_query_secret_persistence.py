from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.research import NetworkResearchRepository, ResearchRepository, ResearchWorkspace
from nika_core.research.models import SourceKind, SourceSpec
from nika_core.research.source_identity import ResearchSourceIdentityError


_CANARY = "QA53_SYNTHETIC_AUTH_QUERY_SECRET_b3e2a619"


def test_auth_query_credential_is_rejected_before_sqlite_persistence(
    tmp_path: Path,
) -> None:
    """QA_ONLY oracle for a common query credential alias omitted by DEV23."""

    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    ResearchRepository(store).upsert_workspace(ResearchWorkspace("ws", "Research"))
    network = NetworkResearchRepository(store)
    locator = f"https://example.com/data?auth={_CANARY}"

    with pytest.raises(ResearchSourceIdentityError) as rejected:
        network.register_source(SourceSpec("qa53-auth", "ws", SourceKind.HTTP, locator))

    assert rejected.value.code == "credentials_forbidden"
    assert _CANARY not in str(rejected.value)

    with store.connection() as conn:
        row = conn.execute(
            "SELECT url FROM research_http_sources WHERE source_id='qa53-auth'"
        ).fetchone()
        serialized = "\n".join(
            str(value)
            for db_row in conn.execute("SELECT * FROM research_http_sources").fetchall()
            for value in db_row
            if value is not None
        )

    assert row is None
    assert _CANARY not in serialized
