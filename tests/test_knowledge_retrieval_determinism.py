from __future__ import annotations

import random
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.research.knowledge import (
    KnowledgeCorpus,
    KnowledgeIngestRequest,
    RetrievalScope,
)

_TIMESTAMP = "2026-09-10T00:00:00+00:00"
_ARTIFACT_KEYS = tuple(f"doc-{index:02d}" for index in range(8))


def _make_store(path: Path) -> SQLiteStore:
    store = SQLiteStore(path)
    store.initialize()
    with store.connection() as conn:
        conn.execute(
            """INSERT INTO research_workspaces(workspace_id, name, created_at, updated_at)
            VALUES (?, ?, ?, ?)""",
            ("ws-a", "A", _TIMESTAMP, _TIMESTAMP),
        )
        conn.execute(
            """INSERT INTO research_sources(
                source_id, workspace_id, kind, locator, created_at, updated_at
            ) VALUES ('fixture-source', 'ws-a', 'local_file', 'approved:ws-a', ?, ?)""",
            (_TIMESTAMP, _TIMESTAMP),
        )
    return store


def _request(artifact_key: str, text: str = "alpha or beta literal gamma") -> KnowledgeIngestRequest:
    return KnowledgeIngestRequest(
        workspace_id="ws-a",
        artifact_key=artifact_key,
        title="Same title",
        media_type="text/plain",
        text=text,
        source_locator="approved:ws-a",
        parser_name="text",
        parser_version="1",
        approved_by="approval:owner",
        source_id="fixture-source",
    )


def _scope() -> RetrievalScope:
    return RetrievalScope(principal_id="user:reader", workspace_ids=("ws-a",))


def _ordered_keys(corpus: KnowledgeCorpus, *, limit: int = 100) -> tuple[str, ...]:
    return tuple(
        hit.provenance.artifact_key
        for hit in corpus.search(_scope(), "alpha beta", limit=limit)
    )


def test_randomized_insertion_order_keeps_tied_ranking_and_limit_prefix(
    tmp_path: Path,
) -> None:
    expected = tuple(sorted(_ARTIFACT_KEYS))

    for seed in range(12):
        order = list(_ARTIFACT_KEYS)
        random.Random(seed).shuffle(order)
        store = _make_store(tmp_path / f"seed-{seed}.db")
        corpus = KnowledgeCorpus(store)
        for artifact_key in order:
            corpus.ingest(_request(artifact_key))

        hits = corpus.search(_scope(), "alpha beta", limit=100)
        assert tuple(hit.provenance.artifact_key for hit in hits) == expected
        assert [hit.rank for hit in hits] == pytest.approx(
            [hits[0].rank] * len(hits), rel=0.0, abs=1e-15
        )
        assert _ordered_keys(corpus, limit=3) == expected[:3]

        restarted = KnowledgeCorpus(SQLiteStore(store.path))
        assert _ordered_keys(restarted) == expected
        assert _ordered_keys(restarted, limit=3) == expected[:3]


def test_duplicate_content_uses_stable_identity_tie_break(tmp_path: Path) -> None:
    corpus = KnowledgeCorpus(_make_store(tmp_path / "duplicate.db"))
    for artifact_key in reversed(_ARTIFACT_KEYS[:4]):
        corpus.ingest(_request(artifact_key, text="same duplicate alpha beta content"))

    hits = corpus.search(_scope(), "alpha beta", limit=100)
    assert tuple(hit.provenance.artifact_key for hit in hits) == tuple(
        sorted(_ARTIFACT_KEYS[:4])
    )
    assert len({hit.text for hit in hits}) == 1


def test_empty_and_fts_special_queries_are_literal_and_deterministic(tmp_path: Path) -> None:
    corpus = KnowledgeCorpus(_make_store(tmp_path / "query.db"))
    corpus.ingest(_request("a-exact", text="alpha or beta literal gamma"))
    corpus.ingest(_request("b-prefix-only", text="alpha or betamax literal gamma"))

    with pytest.raises(ValueError, match="search query must not be empty"):
        corpus.search(_scope(), " \n\t ")

    hits = corpus.search(_scope(), "alpha OR beta*")
    assert tuple(hit.provenance.artifact_key for hit in hits) == ("a-exact",)

    quoted = corpus.search(_scope(), 'alpha OR "beta"')
    assert tuple(hit.provenance.artifact_key for hit in quoted) == ("a-exact",)