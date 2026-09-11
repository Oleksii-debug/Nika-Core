from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.research.knowledge import (
    KnowledgeCorpus,
    KnowledgeIngestRequest,
    RetrievalScope,
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


def _request(artifact_key: str, text: str) -> KnowledgeIngestRequest:
    return KnowledgeIngestRequest(
        workspace_id="ws-a",
        artifact_key=artifact_key,
        title=artifact_key,
        media_type="text/plain",
        text=text,
        source_locator=f"approved:ws-a:{artifact_key}",
        parser_name="text",
        parser_version="1",
        approved_by="approval:owner",
    )


def _scope() -> RetrievalScope:
    return RetrievalScope(principal_id="user:reader", workspace_ids=("ws-a",))


def _keys(corpus: KnowledgeCorpus, query: str) -> tuple[str, ...]:
    return tuple(
        hit.provenance.artifact_key
        for hit in corpus.search(_scope(), query, limit=100)
    )


def _corpus(tmp_path: Path) -> KnowledgeCorpus:
    corpus = KnowledgeCorpus(_make_store(tmp_path / "query-normalization.db"))
    corpus.ingest(_request("a-adjacent", "alpha beta marker"))
    corpus.ingest(_request("b-separated", "alpha x beta marker"))
    corpus.ingest(_request("c-operator-literal", "alpha OR beta NEAR literal marker"))
    corpus.ingest(_request("d-unicode", "Café FOO résumé marker"))
    return corpus


def test_unicode_whitespace_equivalent_to_ascii_term_boundary(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path)
    baseline = _keys(corpus, "alpha beta")
    assert baseline

    equivalent_queries = (
        "alpha\tbeta",
        "alpha\r\nbeta",
        "alpha\u00a0beta",  # NO-BREAK SPACE
        "alpha\u2003beta",  # EM SPACE
        "alpha\u202fbeta",  # NARROW NO-BREAK SPACE
        "alpha\u3000beta",  # IDEOGRAPHIC SPACE
        "alpha\u2028beta",  # LINE SEPARATOR
        "alpha\u2029beta",  # PARAGRAPH SEPARATOR
    )
    for query in equivalent_queries:
        assert _keys(corpus, query) == baseline


@pytest.mark.parametrize(
    "query",
    (
        "\u2028",
        "\u2029",
        " \t\u2028\r\n\u2029 ",
    ),
)
def test_unicode_whitespace_only_query_is_empty(query: str, tmp_path: Path) -> None:
    corpus = _corpus(tmp_path)

    with pytest.raises(ValueError, match="search query must not be empty"):
        corpus.search(_scope(), query)


def test_isolated_punctuation_does_not_create_an_unmatchable_term(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path)
    baseline = _keys(corpus, "alpha beta")
    assert baseline

    for query in ("alpha / beta", "alpha - beta", "alpha ( ) beta"):
        assert _keys(corpus, query) == baseline

    # Attached punctuation keeps the existing unicode61 phrase semantics; a repair
    # must not achieve stability by globally deleting punctuation from user text.
    assert _keys(corpus, "alpha/beta") != baseline


def test_unicode_normalization_and_fts_syntax_like_text_stay_literal(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path)

    canonical = _keys(corpus, "café FOO")
    decomposed_fullwidth = _keys(corpus, "cafe\u0301 ＦＯＯ")
    assert decomposed_fullwidth == canonical == ("d-unicode",)

    quoted_operator = _keys(corpus, 'alpha OR "beta"')
    assert quoted_operator == ("c-operator-literal",)
    assert _keys(corpus, "alpha OR beta*") == quoted_operator
