from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.research.chunking import ChunkPolicy
from nika_core.research.knowledge import (
    CorpusCorruptionError,
    KnowledgeCorpus,
    KnowledgeIngestDisposition,
    KnowledgeIngestRequest,
    KnowledgeVisibility,
    RetrievalScope,
)
from nika_core.research.knowledge_schema import KNOWLEDGE_SCHEMA_VERSION
from nika_core.research.retrieval_evaluation import (
    RetrievalEvaluationCase,
    evaluate_fts_retrieval,
)

_TIMESTAMP = "2026-08-23T00:00:00+00:00"


def _make_store(tmp_path: Path, *workspace_ids: str) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    with store.connection() as conn:
        conn.executemany(
            """INSERT INTO research_workspaces(workspace_id, name, created_at, updated_at)
            VALUES (?, ?, ?, ?)""",
            (
                (workspace_id, workspace_id, _TIMESTAMP, _TIMESTAMP)
                for workspace_id in workspace_ids
            ),
        )
    return store


def _request(**overrides: object) -> KnowledgeIngestRequest:
    values: dict[str, object] = {
        "workspace_id": "ws-a",
        "artifact_key": "artifact-a",
        "title": "Knowledge fixture",
        "media_type": "text/plain",
        "text": "alpha durable retrieval marker",
        "source_locator": "approved:fixture-a",
        "parser_name": "text",
        "parser_version": "1",
        "approved_by": "approval:owner",
    }
    values.update(overrides)
    return KnowledgeIngestRequest(**values)  # type: ignore[arg-type]


def _scope(principal_id: str = "user:reader", *workspaces: str) -> RetrievalScope:
    return RetrievalScope(
        principal_id=principal_id,
        workspace_ids=workspaces or ("ws-a",),
    )


def _register_local_source(
    store: SQLiteStore,
    *,
    source_id: str,
    workspace_id: str,
    locator: str,
) -> None:
    with store.connection() as conn:
        conn.execute(
            """INSERT INTO research_sources(
                source_id, workspace_id, kind, locator, created_at, updated_at
            ) VALUES (?, ?, 'local_file', ?, ?, ?)""",
            (source_id, workspace_id, locator, _TIMESTAMP, _TIMESTAMP),
        )


def _register_http_source(
    store: SQLiteStore,
    *,
    source_id: str,
    workspace_id: str,
    locator: str,
) -> None:
    with store.connection() as conn:
        conn.execute(
            """INSERT INTO research_http_sources(
                source_id, workspace_id, url, freshness, created_at, updated_at
            ) VALUES (?, ?, ?, 'unknown', ?, ?)""",
            (source_id, workspace_id, locator, _TIMESTAMP, _TIMESTAMP),
        )


def test_duplicate_restart_change_and_reversion_create_expected_versions(tmp_path: Path) -> None:
    store = _make_store(tmp_path, "ws-a")
    corpus = KnowledgeCorpus(store)
    first = corpus.ingest(_request(raw_sha256="1" * 64))
    duplicate = corpus.ingest(_request(raw_sha256="1" * 64))

    assert first.disposition is KnowledgeIngestDisposition.CREATED
    assert duplicate.disposition is KnowledgeIngestDisposition.DEDUPLICATED
    assert corpus.version_numbers("ws-a", "artifact-a") == (1,)

    restarted = KnowledgeCorpus(SQLiteStore(store.path))
    second = restarted.ingest(
        _request(text="beta changed retrieval marker", raw_sha256="2" * 64)
    )
    third = restarted.ingest(_request(raw_sha256="1" * 64))

    assert second.version == 2
    assert third.version == 3
    assert second.disposition is KnowledgeIngestDisposition.VERSIONED
    assert third.disposition is KnowledgeIngestDisposition.VERSIONED
    assert restarted.version_numbers("ws-a", "artifact-a") == (1, 2, 3)
    assert restarted.search(_scope(), "alpha")[0].provenance.version == 3
    assert restarted.search(_scope(), "beta") == []


def test_provenance_and_chunk_policy_changes_are_versioned(tmp_path: Path) -> None:
    store = _make_store(tmp_path, "ws-a")
    corpus = KnowledgeCorpus(store)
    base = _request(text="same text marker", raw_sha256="a" * 64)

    assert corpus.ingest(base).version == 1
    assert corpus.ingest(replace(base, parser_version="2")).version == 2
    assert corpus.ingest(replace(base, approved_by="approval:reviewer")).version == 3
    assert corpus.ingest(replace(base, raw_sha256="b" * 64)).version == 4
    assert corpus.ingest(base, chunk_policy=ChunkPolicy(max_chars=64, overlap_chars=8)).version == 5
    assert corpus.version_numbers("ws-a", "artifact-a") == (1, 2, 3, 4, 5)


def test_duplicate_ingest_cannot_silently_change_acl(tmp_path: Path) -> None:
    store = _make_store(tmp_path, "ws-a")
    corpus = KnowledgeCorpus(store)
    restricted = _request(
        visibility=KnowledgeVisibility.RESTRICTED,
        allowed_principals=("user:alice",),
    )
    corpus.ingest(restricted)

    with pytest.raises(ValueError, match="silently mutate knowledge permissions"):
        corpus.ingest(replace(restricted, allowed_principals=("user:bob",)))

    assert corpus.version_numbers("ws-a", "artifact-a") == (1,)
    assert len(corpus.search(_scope("user:alice"), "marker")) == 1
    assert corpus.search(_scope("user:bob"), "marker") == []


def test_new_version_can_change_acl_atomically(tmp_path: Path) -> None:
    store = _make_store(tmp_path, "ws-a")
    corpus = KnowledgeCorpus(store)
    first = _request(
        text="first acl marker",
        visibility=KnowledgeVisibility.RESTRICTED,
        allowed_principals=("user:alice",),
    )
    corpus.ingest(first)
    corpus.ingest(
        replace(
            first,
            text="second acl marker",
            allowed_principals=("user:bob",),
        )
    )

    assert corpus.search(_scope("user:alice"), "second") == []
    bob_hits = corpus.search(_scope("user:bob"), "second")
    assert len(bob_hits) == 1
    assert bob_hits[0].provenance.version == 2


def test_ingest_rejects_cross_workspace_source_identity(tmp_path: Path) -> None:
    store = _make_store(tmp_path, "ws-a", "ws-b")
    _register_local_source(
        store,
        source_id="source-b",
        workspace_id="ws-b",
        locator="file:///workspace-b/private.txt",
    )
    corpus = KnowledgeCorpus(store)

    with pytest.raises(PermissionError, match="crosses workspace boundary"):
        corpus.ingest(
            _request(
                source_id="source-b",
                source_locator="file:///workspace-b/private.txt",
            )
        )

    assert corpus.version_numbers("ws-a", "artifact-a") == ()


def test_ingest_rejects_unknown_ambiguous_or_mismatched_source_identity(tmp_path: Path) -> None:
    store = _make_store(tmp_path, "ws-a")
    corpus = KnowledgeCorpus(store)

    with pytest.raises(ValueError, match="missing or ambiguous"):
        corpus.ingest(_request(source_id="missing"))

    _register_local_source(
        store,
        source_id="source-a",
        workspace_id="ws-a",
        locator="file:///workspace-a/source.txt",
    )
    with pytest.raises(ValueError, match="locator does not match"):
        corpus.ingest(
            _request(
                source_id="source-a",
                source_locator="file:///workspace-a/forged.txt",
            )
        )

    _register_http_source(
        store,
        source_id="source-a",
        workspace_id="ws-a",
        locator="https://example.test/source",
    )
    with pytest.raises(ValueError, match="missing or ambiguous"):
        corpus.ingest(
            _request(
                source_id="source-a",
                source_locator="file:///workspace-a/source.txt",
            )
        )
    assert corpus.version_numbers("ws-a", "artifact-a") == ()


def test_http_source_provenance_is_preserved_from_exact_durable_identity(tmp_path: Path) -> None:
    store = _make_store(tmp_path, "ws-a")
    locator = "https://example.test/research"
    _register_http_source(
        store,
        source_id="http-a",
        workspace_id="ws-a",
        locator=locator,
    )
    corpus = KnowledgeCorpus(store)
    corpus.ingest(_request(source_id="http-a", source_locator=locator))

    hit = corpus.search(_scope(), "marker")[0]
    assert hit.provenance.source_id == "http-a"
    assert hit.provenance.source_locator == locator


def test_workspace_and_restricted_acl_filters_apply_before_returning_hits(tmp_path: Path) -> None:
    store = _make_store(tmp_path, "ws-a", "ws-b")
    corpus = KnowledgeCorpus(store)
    corpus.ingest(_request(artifact_key="public-a", text="shared filter marker"))
    corpus.ingest(
        _request(
            workspace_id="ws-b",
            artifact_key="public-b",
            text="shared filter marker",
        )
    )
    corpus.ingest(
        _request(
            artifact_key="private-a",
            text="shared filter marker",
            visibility=KnowledgeVisibility.RESTRICTED,
            allowed_principals=("user:alice",),
        )
    )

    reader_hits = corpus.search(_scope("user:reader", "ws-a"), "shared filter")
    assert {hit.provenance.artifact_key for hit in reader_hits} == {"public-a"}
    alice_hits = corpus.search(_scope("user:alice", "ws-a"), "shared filter")
    assert {hit.provenance.artifact_key for hit in alice_hits} == {
        "private-a",
        "public-a",
    }


def test_fts_query_is_literal_and_tie_order_is_deterministic(tmp_path: Path) -> None:
    store = _make_store(tmp_path, "ws-a")
    corpus = KnowledgeCorpus(store)
    corpus.ingest(
        _request(
            artifact_key="b-artifact",
            title="Same title",
            text="alpha or beta literal",
        )
    )
    corpus.ingest(
        _request(
            artifact_key="a-artifact",
            title="Same title",
            text="alpha or beta literal",
        )
    )

    hits = corpus.search(_scope(), "alpha OR beta")
    assert [hit.provenance.artifact_key for hit in hits] == [
        "a-artifact",
        "b-artifact",
    ]
    assert corpus.search(_scope(), "alpha OR missing") == []


def test_chunk_boundaries_hashes_and_policy_are_returned_as_provenance(tmp_path: Path) -> None:
    store = _make_store(tmp_path, "ws-a")
    corpus = KnowledgeCorpus(store)
    text = " ".join(f"marker-{index}" for index in range(30))
    policy = ChunkPolicy(max_chars=64, overlap_chars=8)
    corpus.ingest(_request(text=text), chunk_policy=policy)

    hits = corpus.search(_scope(), "marker-10")
    assert hits
    for hit in hits:
        provenance = hit.provenance
        assert text[provenance.start_char : provenance.end_char] == hit.text
        assert hashlib.sha256(hit.text.encode()).hexdigest() == provenance.chunk_sha256
        assert provenance.chunk_max_chars == 64
        assert provenance.chunk_overlap_chars == 8


def test_search_exposes_only_current_version(tmp_path: Path) -> None:
    store = _make_store(tmp_path, "ws-a")
    corpus = KnowledgeCorpus(store)
    corpus.ingest(_request(text="obsoletekeyword marker"))
    corpus.ingest(_request(text="currentkeyword marker"))

    assert corpus.search(_scope(), "obsoletekeyword") == []
    hit = corpus.search(_scope(), "currentkeyword")[0]
    assert hit.provenance.version == 2


def test_version_hash_corruption_fails_closed_on_search_and_integrity(tmp_path: Path) -> None:
    store = _make_store(tmp_path, "ws-a")
    corpus = KnowledgeCorpus(store)
    corpus.ingest(_request())
    with store.connection() as conn:
        conn.execute(
            """UPDATE knowledge_versions SET normalized_text='tampered marker'
            WHERE workspace_id='ws-a' AND artifact_key='artifact-a' AND version=1"""
        )

    with pytest.raises(CorpusCorruptionError, match="version hash mismatch"):
        corpus.search(_scope(), "marker")
    with pytest.raises(CorpusCorruptionError, match="version hash mismatch"):
        corpus.verify_integrity()


def test_chunk_boundary_corruption_fails_integrity(tmp_path: Path) -> None:
    store = _make_store(tmp_path, "ws-a")
    corpus = KnowledgeCorpus(store)
    corpus.ingest(_request())
    with store.connection() as conn:
        conn.execute("UPDATE knowledge_chunks SET end_char=end_char+1000")

    with pytest.raises(CorpusCorruptionError, match="boundaries are invalid"):
        corpus.verify_integrity()


def test_stale_fts_title_fails_closed_before_returning_material(tmp_path: Path) -> None:
    store = _make_store(tmp_path, "ws-a")
    corpus = KnowledgeCorpus(store)
    corpus.ingest(_request())
    with store.connection() as conn:
        conn.execute("UPDATE knowledge_fts SET title='forgedtitle'")

    with pytest.raises(CorpusCorruptionError, match="FTS title"):
        corpus.search(_scope(), "forgedtitle")
    with pytest.raises(CorpusCorruptionError, match="FTS metadata"):
        corpus.verify_integrity()


def test_orphan_fts_row_fails_integrity(tmp_path: Path) -> None:
    store = _make_store(tmp_path, "ws-a")
    corpus = KnowledgeCorpus(store)
    corpus.ingest(_request())
    with store.connection() as conn:
        conn.execute(
            """INSERT INTO knowledge_fts(
                workspace_id, artifact_key, version, ordinal, chunk_id, title, body
            ) VALUES ('ws-a', 'artifact-a', '1', '99', 'orphan', 'orphan', 'orphan marker')"""
        )

    with pytest.raises(CorpusCorruptionError, match="no authoritative chunk"):
        corpus.verify_integrity()


def test_ingest_transaction_rolls_back_after_late_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _make_store(tmp_path, "ws-a")
    corpus = KnowledgeCorpus(store)

    def fail_acl(*_args: object) -> None:
        raise RuntimeError("injected late failure")

    monkeypatch.setattr(KnowledgeCorpus, "_replace_acl", staticmethod(fail_acl))
    with pytest.raises(RuntimeError, match="injected late failure"):
        corpus.ingest(_request())

    with store.connection() as conn:
        version_count = conn.execute("SELECT COUNT(*) FROM knowledge_versions").fetchone()[0]
        chunk_count = conn.execute("SELECT COUNT(*) FROM knowledge_chunks").fetchone()[0]
        fts_count = conn.execute("SELECT COUNT(*) FROM knowledge_fts").fetchone()[0]
    assert (version_count, chunk_count, fts_count) == (0, 0, 0)


def test_concurrent_duplicate_ingest_creates_one_version(tmp_path: Path) -> None:
    store = _make_store(tmp_path, "ws-a")
    barrier = Barrier(2)
    request = _request()

    def ingest_once() -> KnowledgeIngestDisposition:
        barrier.wait()
        local = KnowledgeCorpus(SQLiteStore(store.path))
        return local.ingest(request).disposition

    with ThreadPoolExecutor(max_workers=2) as executor:
        dispositions = tuple(executor.map(lambda _index: ingest_once(), range(2)))

    assert set(dispositions) == {
        KnowledgeIngestDisposition.CREATED,
        KnowledgeIngestDisposition.DEDUPLICATED,
    }
    corpus = KnowledgeCorpus(SQLiteStore(store.path))
    assert corpus.version_numbers("ws-a", "artifact-a") == (1,)
    assert corpus.verify_integrity().versions_checked == 1


def test_legacy_corpus_is_backfilled_without_removing_old_rows(tmp_path: Path) -> None:
    store = _make_store(tmp_path, "ws-a")
    with store.connection() as conn:
        conn.execute("DROP TABLE knowledge_fts")
        conn.execute("DROP TABLE knowledge_acl")
        conn.execute("DROP TABLE knowledge_chunks")
        conn.execute("DROP TABLE knowledge_versions")
        conn.execute("DROP TABLE knowledge_artifacts")
        conn.execute("DROP TABLE knowledge_schema_migrations")
        conn.execute(
            """INSERT INTO corpus_documents(
                document_id, workspace_id, normalized_sha256, title, media_type,
                normalized_text, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                "legacy-doc",
                "ws-a",
                hashlib.sha256(b"legacy migration marker").hexdigest(),
                "Legacy",
                "text/plain",
                "legacy migration marker",
                _TIMESTAMP,
            ),
        )

    store.initialize()
    corpus = KnowledgeCorpus(store)
    hit = corpus.search(_scope(), "legacy migration")[0]
    assert hit.provenance.artifact_key == "legacy:legacy-doc"
    assert hit.provenance.start_char == 0
    assert hit.provenance.end_char == len("legacy migration marker")
    with store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM corpus_documents WHERE document_id='legacy-doc'"
        ).fetchone()[0] == 1


def test_future_knowledge_schema_fails_closed(tmp_path: Path) -> None:
    store = _make_store(tmp_path, "ws-a")
    with store.connection() as conn:
        conn.execute(
            "INSERT INTO knowledge_schema_migrations(version, applied_at) VALUES (?, ?)",
            (KNOWLEDGE_SCHEMA_VERSION + 1, _TIMESTAMP),
        )

    with pytest.raises(RuntimeError, match="newer than supported"):
        store.initialize()


def test_retrieval_evaluation_counts_distinct_artifacts_and_respects_acl(tmp_path: Path) -> None:
    store = _make_store(tmp_path, "ws-a")
    corpus = KnowledgeCorpus(store)
    repeated = " ".join("evaluationmarker" for _ in range(80))
    corpus.ingest(
        _request(artifact_key="expected", text=repeated),
        chunk_policy=ChunkPolicy(max_chars=64, overlap_chars=8),
    )
    corpus.ingest(
        _request(
            artifact_key="restricted",
            text="evaluationmarker hidden",
            visibility=KnowledgeVisibility.RESTRICTED,
            allowed_principals=("user:alice",),
        )
    )
    case = RetrievalEvaluationCase(
        case_id="fts-visible",
        scope=_scope("user:reader"),
        query="evaluationmarker",
        expected_artifact_keys=("expected",),
    )

    report = evaluate_fts_retrieval(corpus, (case,), limit=20)
    assert report.recall_at_limit == 1.0
    assert report.hit_at_1_rate == 1.0
    assert report.cases[0].retrieved_artifact_keys == ("expected",)


def test_search_and_evaluation_reject_boolean_limits(tmp_path: Path) -> None:
    store = _make_store(tmp_path, "ws-a")
    corpus = KnowledgeCorpus(store)
    corpus.ingest(_request())
    case = RetrievalEvaluationCase(
        case_id="limit",
        scope=_scope(),
        query="marker",
        expected_artifact_keys=("artifact-a",),
    )

    with pytest.raises(ValueError, match="integer between 1 and 100"):
        corpus.search(_scope(), "marker", limit=True)
    with pytest.raises(ValueError, match="integer between 1 and 100"):
        evaluate_fts_retrieval(corpus, (case,), limit=True)
