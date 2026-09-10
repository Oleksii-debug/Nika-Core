from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.research.knowledge import (
    CorpusCorruptionError,
    KnowledgeCorpus,
    KnowledgeIngestRequest,
    RetrievalScope,
)
from nika_core.research.retrieval_provenance import (
    RetrievalEvidenceStatus,
    StaleRetrievalEvidenceError,
    deserialize_retrieval_provenance,
    restore_retrieval_provenance,
    serialize_retrieval_provenance,
)

_TIMESTAMP = "2026-09-10T00:00:00+00:00"


def _make_store(path: Path) -> SQLiteStore:
    store = SQLiteStore(path)
    store.initialize()
    with store.connection() as conn:
        conn.execute(
            """INSERT INTO research_workspaces(workspace_id, name, created_at, updated_at)
            VALUES (?, ?, ?, ?)""",
            ("ws-a", "A", _TIMESTAMP, _TIMESTAMP),
        )
    return store


def _request(**overrides: object) -> KnowledgeIngestRequest:
    values: dict[str, object] = {
        "workspace_id": "ws-a",
        "artifact_key": "artifact-a",
        "title": "Retrieval provenance fixture",
        "media_type": "text/plain",
        "text": "alpha durable provenance marker",
        "source_locator": "approved:fixture-a",
        "parser_name": "text",
        "parser_version": "1",
        "approved_by": "approval:owner",
    }
    values.update(overrides)
    return KnowledgeIngestRequest(**values)  # type: ignore[arg-type]


def _scope() -> RetrievalScope:
    return RetrievalScope(principal_id="user:reader", workspace_ids=("ws-a",))


def _restart(store: SQLiteStore) -> SQLiteStore:
    restarted = SQLiteStore(store.path)
    restarted.initialize()
    return restarted


def test_serialized_provenance_round_trips_and_revalidates_after_restart(
    tmp_path: Path,
) -> None:
    store = _make_store(tmp_path / "nika.db")
    corpus = KnowledgeCorpus(store)
    corpus.ingest(_request())
    hit = corpus.search(_scope(), "provenance")[0]

    payload = serialize_retrieval_provenance(hit.provenance)
    decoded = deserialize_retrieval_provenance(payload)
    restored, status = restore_retrieval_provenance(_restart(store), payload)

    assert decoded == hit.provenance
    assert restored == hit.provenance
    assert status is RetrievalEvidenceStatus.CURRENT
    assert serialize_retrieval_provenance(decoded) == payload
    assert json.loads(payload)["provenance"]["source_id"] is None


def test_old_serialized_evidence_cannot_masquerade_after_corpus_update(
    tmp_path: Path,
) -> None:
    store = _make_store(tmp_path / "nika.db")
    corpus = KnowledgeCorpus(store)
    first = _request()
    corpus.ingest(first)
    old_hit = corpus.search(_scope(), "alpha")[0]
    old_payload = serialize_retrieval_provenance(old_hit.provenance)

    corpus.ingest(replace(first, text="beta replacement provenance marker"))
    restarted = _restart(store)

    with pytest.raises(StaleRetrievalEvidenceError, match="superseded"):
        restore_retrieval_provenance(restarted, old_payload)

    historical, status = restore_retrieval_provenance(
        restarted,
        old_payload,
        require_current=False,
    )
    assert historical.version == 1
    assert status is RetrievalEvidenceStatus.SUPERSEDED

    current_hit = KnowledgeCorpus(restarted).search(_scope(), "beta")[0]
    current_payload = serialize_retrieval_provenance(current_hit.provenance)
    current, current_status = restore_retrieval_provenance(restarted, current_payload)
    assert current.version == 2
    assert current_status is RetrievalEvidenceStatus.CURRENT


def test_registered_source_change_invalidates_old_retrieval_evidence(
    tmp_path: Path,
) -> None:
    store = _make_store(tmp_path / "nika.db")
    original_locator = "file:///workspace/source-a.txt"
    with store.connection() as conn:
        conn.execute(
            """INSERT INTO research_sources(
                source_id, workspace_id, kind, locator, created_at, updated_at
            ) VALUES (?, ?, 'local_file', ?, ?, ?)""",
            ("source-a", "ws-a", original_locator, _TIMESTAMP, _TIMESTAMP),
        )
    corpus = KnowledgeCorpus(store)
    corpus.ingest(
        _request(source_id="source-a", source_locator=original_locator)
    )
    payload = serialize_retrieval_provenance(
        corpus.search(_scope(), "provenance")[0].provenance
    )

    with store.connection() as conn:
        conn.execute(
            """UPDATE research_sources
            SET locator='file:///workspace/rebound.txt'
            WHERE source_id='source-a'"""
        )

    with pytest.raises(CorpusCorruptionError, match="source identity no longer matches"):
        restore_retrieval_provenance(
            _restart(store),
            payload,
            require_current=False,
        )


def test_tampered_serialized_position_fails_authoritative_revalidation(
    tmp_path: Path,
) -> None:
    store = _make_store(tmp_path / "nika.db")
    corpus = KnowledgeCorpus(store)
    corpus.ingest(_request())
    payload = serialize_retrieval_provenance(
        corpus.search(_scope(), "provenance")[0].provenance
    )
    decoded = json.loads(payload)
    decoded["provenance"]["start_char"] += 1
    tampered = json.dumps(decoded, separators=(",", ":"), sort_keys=True)

    with pytest.raises(CorpusCorruptionError, match="start_char"):
        restore_retrieval_provenance(_restart(store), tampered)
