from __future__ import annotations

import hashlib
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


def _drop_knowledge_schema(store: SQLiteStore) -> None:
    with store.connection() as conn:
        conn.execute("DROP TABLE knowledge_fts")
        conn.execute("DROP TABLE knowledge_acl")
        conn.execute("DROP TABLE knowledge_chunks")
        conn.execute("DROP TABLE knowledge_versions")
        conn.execute("DROP TABLE knowledge_artifacts")
        conn.execute("DROP TABLE knowledge_schema_migrations")


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


def test_legacy_migration_never_copies_credential_locator_into_knowledge(
    tmp_path: Path,
) -> None:
    store, _corpus = _make_corpus(tmp_path)
    legacy_text = "legacy credential provenance marker"
    secret_locator = f"https://example.com/legacy?access_token={_CANARY}"

    with store.connection() as conn:
        conn.execute(
            """INSERT INTO research_sources(
                source_id, workspace_id, kind, locator, created_at, updated_at
            ) VALUES (?, ?, 'local_file', ?, ?, ?)""",
            ("legacy-source", "ws-a", secret_locator, _TIMESTAMP, _TIMESTAMP),
        )
        conn.execute(
            """INSERT INTO corpus_documents(
                document_id, workspace_id, normalized_sha256, title, media_type,
                normalized_text, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                "legacy-secret-doc",
                "ws-a",
                hashlib.sha256(legacy_text.encode()).hexdigest(),
                "Legacy secret fixture",
                "text/plain",
                legacy_text,
                _TIMESTAMP,
            ),
        )
        conn.execute(
            """INSERT INTO corpus_origins(document_id, source_id, locator, observed_at)
            VALUES (?, ?, ?, ?)""",
            ("legacy-secret-doc", "legacy-source", secret_locator, _TIMESTAMP),
        )

    _drop_knowledge_schema(store)
    try:
        store.initialize()
    except Exception as exc:  # noqa: BLE001 - safe migration may fail closed
        assert _CANARY not in str(exc)
        assert _CANARY not in repr(exc)
        return

    with store.connection() as conn:
        migrated = conn.execute(
            """SELECT source_locator FROM knowledge_versions
            WHERE workspace_id=? AND artifact_key=? AND version=1""",
            ("ws-a", "legacy:legacy-secret-doc"),
        ).fetchone()
    assert migrated is not None
    assert _CANARY not in migrated["source_locator"]


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
