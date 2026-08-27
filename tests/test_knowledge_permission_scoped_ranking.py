from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.research.knowledge import (
    KnowledgeCorpus,
    KnowledgeIngestRequest,
    KnowledgeVisibility,
    RetrievalScope,
)

_TIMESTAMP = "2026-08-23T00:00:00+00:00"


def _make_store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    with store.connection() as conn:
        conn.executemany(
            """INSERT INTO research_workspaces(workspace_id, name, created_at, updated_at)
            VALUES (?, ?, ?, ?)""",
            (
                ("ws-a", "A", _TIMESTAMP, _TIMESTAMP),
                ("ws-b", "B", _TIMESTAMP, _TIMESTAMP),
            ),
        )
    return store


def _request(
    *,
    workspace_id: str,
    artifact_key: str,
    text: str,
    visibility: KnowledgeVisibility = KnowledgeVisibility.WORKSPACE,
    allowed_principals: tuple[str, ...] = (),
) -> KnowledgeIngestRequest:
    return KnowledgeIngestRequest(
        workspace_id=workspace_id,
        artifact_key=artifact_key,
        title=artifact_key,
        media_type="text/plain",
        text=text,
        source_locator=f"approved:{workspace_id}:{artifact_key}",
        parser_name="text",
        parser_version="1",
        approved_by="approval:owner",
        visibility=visibility,
        allowed_principals=allowed_principals,
    )


def _scope(principal_id: str, *workspace_ids: str) -> RetrievalScope:
    return RetrievalScope(principal_id=principal_id, workspace_ids=workspace_ids)


def _seed_balanced_pair(corpus: KnowledgeCorpus) -> None:
    corpus.ingest(
        _request(
            workspace_id="ws-a",
            artifact_key="a-alpha-heavy",
            text=" ".join(["alpha"] * 10 + ["beta"]),
        )
    )
    corpus.ingest(
        _request(
            workspace_id="ws-a",
            artifact_key="b-beta-heavy",
            text=" ".join(["alpha"] + ["beta"] * 10),
        )
    )


def _ranking(corpus: KnowledgeCorpus, principal_id: str) -> list[tuple[str, float]]:
    hits = corpus.search(_scope(principal_id, "ws-a"), "alpha beta")
    return [(hit.provenance.artifact_key, hit.rank) for hit in hits]


def _assert_same_ranking(
    actual: list[tuple[str, float]],
    expected: list[tuple[str, float]],
) -> None:
    assert [item[0] for item in actual] == [item[0] for item in expected]
    assert [item[1] for item in actual] == pytest.approx(
        [item[1] for item in expected],
        rel=0.0,
        abs=1e-15,
    )


def test_other_workspace_cannot_change_authorized_bm25_ranking(tmp_path: Path) -> None:
    store = _make_store(tmp_path)
    corpus = KnowledgeCorpus(store)
    _seed_balanced_pair(corpus)
    baseline = _ranking(corpus, "user:reader")

    assert [item[0] for item in baseline] == ["a-alpha-heavy", "b-beta-heavy"]
    assert baseline[0][1] == pytest.approx(baseline[1][1], rel=0.0, abs=1e-15)

    for index in range(100):
        corpus.ingest(
            _request(
                workspace_id="ws-b",
                artifact_key=f"private-workspace-{index:03d}",
                text=f"alpha inaccessible filler {index}",
            )
        )

    _assert_same_ranking(_ranking(corpus, "user:reader"), baseline)


def test_restricted_documents_cannot_change_unauthorized_bm25_ranking(
    tmp_path: Path,
) -> None:
    store = _make_store(tmp_path)
    corpus = KnowledgeCorpus(store)
    _seed_balanced_pair(corpus)
    baseline = _ranking(corpus, "user:reader")

    for index in range(100):
        corpus.ingest(
            _request(
                workspace_id="ws-a",
                artifact_key=f"alice-only-{index:03d}",
                text=f"alpha restrictedonly filler {index}",
                visibility=KnowledgeVisibility.RESTRICTED,
                allowed_principals=("user:alice",),
            )
        )

    _assert_same_ranking(_ranking(corpus, "user:reader"), baseline)
    assert corpus.search(_scope("user:reader", "ws-a"), "restrictedonly") == []
    alice_hits = corpus.search(
        _scope("user:alice", "ws-a"),
        "restrictedonly",
        limit=100,
    )
    assert len(alice_hits) == 100
    assert all(hit.provenance.artifact_key.startswith("alice-only-") for hit in alice_hits)
