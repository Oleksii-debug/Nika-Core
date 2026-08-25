from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.research.knowledge import KnowledgeCorpus, KnowledgeIngestRequest

_TIMESTAMP = "2026-08-25T00:00:00+00:00"
_CANARY = "nika_qa_locator_secret_canary"


def _make_corpus(tmp_path: Path) -> tuple[SQLiteStore, KnowledgeCorpus]:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    with store.connection() as conn:
        conn.execute(
            """INSERT INTO research_workspaces(workspace_id, name, created_at, updated_at)
            VALUES (?, ?, ?, ?)""",
            ("ws-a", "Research A", _TIMESTAMP, _TIMESTAMP),
        )
    return store, KnowledgeCorpus(store)


def _request(source_locator: str, *, artifact_key: str) -> KnowledgeIngestRequest:
    return KnowledgeIngestRequest(
        workspace_id="ws-a",
        artifact_key=artifact_key,
        title="Credential provenance fixture",
        media_type="text/plain",
        text="credential provenance marker",
        source_locator=source_locator,
        parser_name="text",
        parser_version="1",
        approved_by="approval:owner",
        source_id=None,
    )


@pytest.mark.parametrize(
    ("source_locator", "artifact_key"),
    (
        (f"https://example.com/document?access_token={_CANARY}", "query-secret"),
        (f"https://reader:{_CANARY}@example.com/document", "userinfo-secret"),
    ),
)
def test_unregistered_credential_locator_never_becomes_durable_provenance(
    tmp_path: Path,
    source_locator: str,
    artifact_key: str,
) -> None:
    store, corpus = _make_corpus(tmp_path)

    try:
        corpus.ingest(_request(source_locator, artifact_key=artifact_key))
    except Exception as exc:  # noqa: BLE001 - oracle accepts any safe fail-closed rejection
        assert _CANARY not in str(exc)
        assert _CANARY not in repr(exc)

    with store.connection() as conn:
        durable_locators = tuple(
            row["source_locator"]
            for row in conn.execute(
                "SELECT source_locator FROM knowledge_versions WHERE workspace_id=?",
                ("ws-a",),
            ).fetchall()
        )

    assert all(_CANARY not in locator for locator in durable_locators)


def test_unregistered_opaque_locator_remains_supported(tmp_path: Path) -> None:
    store, corpus = _make_corpus(tmp_path)
    locator = "approved:fixture-without-registered-source"

    result = corpus.ingest(_request(locator, artifact_key="opaque-provenance"))

    assert result.version == 1
    with store.connection() as conn:
        persisted = conn.execute(
            """SELECT source_locator FROM knowledge_versions
            WHERE workspace_id=? AND artifact_key=? AND version=?""",
            ("ws-a", "opaque-provenance", 1),
        ).fetchone()
    assert persisted is not None
    assert persisted["source_locator"] == locator
