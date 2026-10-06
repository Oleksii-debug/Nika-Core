from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.research.models import (
    FreshnessState,
    ResearchEvidence,
    ResearchResultItem,
    ResearchResultSet,
    SourceKind,
)
from nika_core.research.review import (
    ResearchCardService,
    ResearchReviewRepository,
    ResearchReviewState,
)


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    with store.connection() as conn:
        conn.execute(
            """INSERT INTO research_workspaces(workspace_id, name, created_at, updated_at)
            VALUES ('ws', 'Research', '2026-08-20T00:00:00+00:00', '2026-08-20T00:00:00+00:00')"""
        )
        conn.execute(
            """INSERT INTO research_workspaces(workspace_id, name, created_at, updated_at)
            VALUES ('other', 'Other', '2026-08-20T00:00:00+00:00', '2026-08-20T00:00:00+00:00')"""
        )
        conn.execute(
            """INSERT INTO corpus_documents(
                document_id, workspace_id, normalized_sha256, title, media_type,
                normalized_text, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                "doc-1",
                "ws",
                "a" * 64,
                "Українська можливість",
                "text/plain",
                "Детермінований корпус українською мовою.",
                "2026-08-20T00:00:00+00:00",
            ),
        )
    return store


def _result_set() -> ResearchResultSet:
    return ResearchResultSet(
        result_set_id="results-1",
        workspace_id="ws",
        query="українська можливість",
        created_at="2026-08-20T01:00:00+00:00",
        items=(
            ResearchResultItem(
                ordinal=0,
                document_id="doc-1",
                title="Українська можливість",
                snippet="Детермінований корпус українською мовою.",
                rank=-1.25,
                why_matched="Literal-token full-text match for: українська можливість",
                evidence=(
                    ResearchEvidence(
                        source_id="source-1",
                        source_kind=SourceKind.HTTP,
                        locator="https://example.org/opportunity",
                        observed_at="2026-08-20T00:30:00+00:00",
                        freshness=FreshnessState.CURRENT,
                    ),
                ),
            ),
        ),
    )


def test_review_defaults_to_unreviewed_and_survives_restart(tmp_path: Path) -> None:
    store = _store(tmp_path)
    repository = ResearchReviewRepository(store)

    initial = repository.get_review(workspace_id="ws", document_id="doc-1")
    assert initial.state is ResearchReviewState.UNREVIEWED
    assert initial.updated_at is None

    saved = repository.set_review(
        workspace_id="ws",
        document_id="doc-1",
        state=ResearchReviewState.SAVED,
        note="Перевірити дедлайн вручну",
    )
    assert saved.state is ResearchReviewState.SAVED

    restarted = ResearchReviewRepository(SQLiteStore(store.path))
    loaded = restarted.get_review(workspace_id="ws", document_id="doc-1")
    assert loaded.state is ResearchReviewState.SAVED
    assert loaded.note == "Перевірити дедлайн вручну"
    assert loaded.updated_at is not None


def test_review_updates_are_audited_and_identical_write_is_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    repository = ResearchReviewRepository(store)

    repository.set_review(
        workspace_id="ws",
        document_id="doc-1",
        state=ResearchReviewState.SAVED,
        note="keep",
    )
    repository.set_review(
        workspace_id="ws",
        document_id="doc-1",
        state=ResearchReviewState.SAVED,
        note="keep",
    )
    repository.set_review(
        workspace_id="ws",
        document_id="doc-1",
        state=ResearchReviewState.DISMISSED,
        note="out of scope",
    )

    with store.connection() as conn:
        rows = conn.execute(
            """SELECT payload_json FROM audit_events
            WHERE event_type='research.review.changed'
            ORDER BY event_id"""
        ).fetchall()
    assert len(rows) == 2
    assert '"previous_state": "unreviewed"' in rows[0]["payload_json"]
    assert '"previous_state": "saved"' in rows[1]["payload_json"]


def test_review_fails_closed_for_unknown_or_cross_workspace_document(tmp_path: Path) -> None:
    repository = ResearchReviewRepository(_store(tmp_path))

    with pytest.raises(KeyError, match="unknown corpus document"):
        repository.get_review(workspace_id="ws", document_id="missing")
    with pytest.raises(ValueError, match="does not belong"):
        repository.set_review(
            workspace_id="other",
            document_id="doc-1",
            state=ResearchReviewState.SAVED,
        )
    with pytest.raises(ValueError, match="4000"):
        repository.set_review(
            workspace_id="ws",
            document_id="doc-1",
            state=ResearchReviewState.SAVED,
            note="x" * 4001,
        )


def test_cards_and_plain_text_report_preserve_review_and_provenance(tmp_path: Path) -> None:
    store = _store(tmp_path)
    reviews = ResearchReviewRepository(store)
    reviews.set_review(
        workspace_id="ws",
        document_id="doc-1",
        state=ResearchReviewState.SAVED,
        note="важливий доказ",
    )

    service = ResearchCardService(reviews)
    report = service.accessible_report(_result_set())

    assert len(report.cards) == 1
    assert report.cards[0].review.state is ResearchReviewState.SAVED
    assert report.cards[0].evidence[0].locator == "https://example.org/opportunity"
    assert "Research results" in report.text
    assert "Result 1: Українська можливість" in report.text
    assert "Review: saved" in report.text
    assert "Review note: важливий доказ" in report.text
    assert "Source ID: source-1" in report.text
    assert "Source kind: http" in report.text
    assert "Freshness: current" in report.text
    assert "Location: https://example.org/opportunity" in report.text
    assert "Observed: 2026-08-20T00:30:00+00:00" in report.text


def test_report_order_is_result_order_not_review_update_order(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with store.connection() as conn:
        conn.execute(
            """INSERT INTO corpus_documents(
                document_id, workspace_id, normalized_sha256, title, media_type,
                normalized_text, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                "doc-2",
                "ws",
                "b" * 64,
                "Second",
                "text/plain",
                "Second body",
                "2026-08-20T00:01:00+00:00",
            ),
        )
    result = _result_set()
    second = ResearchResultItem(
        ordinal=1,
        document_id="doc-2",
        title="Second",
        snippet="Second body",
        rank=-0.5,
        why_matched="literal",
        evidence=(),
    )
    two_items = ResearchResultSet(
        result_set_id=result.result_set_id,
        workspace_id=result.workspace_id,
        query=result.query,
        items=(result.items[0], second),
        created_at=result.created_at,
    )
    reviews = ResearchReviewRepository(store)
    reviews.set_review(
        workspace_id="ws",
        document_id="doc-2",
        state=ResearchReviewState.DISMISSED,
    )
    reviews.set_review(
        workspace_id="ws",
        document_id="doc-1",
        state=ResearchReviewState.SAVED,
    )

    report = ResearchCardService(reviews).accessible_report(two_items)
    assert [card.document_id for card in report.cards] == ["doc-1", "doc-2"]
    assert report.text.index("Result 1: Українська можливість") < report.text.index(
        "Result 2: Second"
    )
