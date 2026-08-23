from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.research.knowledge import KnowledgeCorpus, KnowledgeIngestRequest


def test_ingest_rejects_source_identity_owned_by_another_workspace(tmp_path: Path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    with store.connection() as conn:
        conn.executemany(
            """INSERT INTO research_workspaces(workspace_id, name, created_at, updated_at)
            VALUES (?, ?, '2026-08-23T00:00:00+00:00', '2026-08-23T00:00:00+00:00')""",
            (("ws-a", "A"), ("ws-b", "B")),
        )
        conn.execute(
            """INSERT INTO research_sources(
                source_id, workspace_id, kind, locator, created_at, updated_at
            ) VALUES (
                'source-b', 'ws-b', 'local_file', 'file:///workspace-b/private.txt',
                '2026-08-23T00:00:00+00:00', '2026-08-23T00:00:00+00:00'
            )"""
        )

    corpus = KnowledgeCorpus(store)
    request = KnowledgeIngestRequest(
        workspace_id="ws-a",
        artifact_key="artifact-a",
        title="Cross-workspace provenance fixture",
        media_type="text/plain",
        text="aud03 provenance marker",
        source_id="source-b",
        source_locator="file:///workspace-b/private.txt",
        parser_name="text",
        parser_version="1",
        approved_by="approval:aud03",
    )

    with pytest.raises((ValueError, PermissionError)):
        corpus.ingest(request)

    assert corpus.version_numbers("ws-a", "artifact-a") == ()
