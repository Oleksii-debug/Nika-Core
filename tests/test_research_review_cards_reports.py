from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

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
    AccessibleResearchReport,
    ResearchCard,
    ResearchCardService,
    ResearchReview,
    ResearchReviewRepository,
    ResearchReviewState,
    render_accessible_report_text,
)

_PUBLIC_SOURCE_1 = (
    "source-sha256:"
    "ffa6a744d78d28386438b4a8e8ee32f56718633735057b1b5ddfb5d224d45d98"
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


def test_concurrent_review_writes_preserve_audit_predecessor_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    repository = ResearchReviewRepository(store)
    original_get_review = repository.get_review
    read_barrier = Barrier(2)

    def interleaved_get_review(
        *,
        workspace_id: str,
        document_id: str,
    ) -> ResearchReview:
        current = original_get_review(
            workspace_id=workspace_id,
            document_id=document_id,
        )
        read_barrier.wait(timeout=5)
        return current

    monkeypatch.setattr(repository, "get_review", interleaved_get_review)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (
            pool.submit(
                repository.set_review,
                workspace_id="ws",
                document_id="doc-1",
                state=ResearchReviewState.SAVED,
                note="saved",
            ),
            pool.submit(
                repository.set_review,
                workspace_id="ws",
                document_id="doc-1",
                state=ResearchReviewState.DISMISSED,
                note="dismissed",
            ),
        )
        for future in futures:
            future.result(timeout=5)

    with store.connection() as conn:
        rows = conn.execute(
            """SELECT payload_json FROM audit_events
            WHERE event_type='research.review.changed'
            ORDER BY event_id"""
        ).fetchall()
    payloads = [json.loads(row["payload_json"]) for row in rows]
    assert len(payloads) == 2
    assert payloads[0]["previous_state"] == ResearchReviewState.UNREVIEWED.value
    assert payloads[1]["previous_state"] == payloads[0]["state"]

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
    assert report.cards[0].evidence[0].source_id == "source-1"
    assert report.cards[0].evidence[0].locator == "https://example.org/opportunity"
    assert "Research results" in report.text
    assert "Result 1: Українська можливість" in report.text
    assert "Review: saved" in report.text
    assert "Review note: важливий доказ" in report.text
    assert f"Source ID: {_PUBLIC_SOURCE_1}" in report.text
    assert "Source ID: source-1" not in report.text
    assert "Source kind: http" in report.text
    assert "Freshness: current" in report.text
    assert "Location: http-source" in report.text
    assert "Observed: 2026-08-20T00:30:00+00:00" in report.text


def test_plain_text_report_redacts_http_and_local_raw_locators(tmp_path: Path) -> None:
    base = _result_set()
    item = base.items[0]
    hostile_item = ResearchResultItem(
        ordinal=item.ordinal,
        document_id=item.document_id,
        title=item.title,
        snippet=item.snippet,
        rank=item.rank,
        why_matched=item.why_matched,
        evidence=(
            ResearchEvidence(
                source_id="http-source-id",
                source_kind=SourceKind.HTTP,
                locator=(
                    "https://resolver-user:resolver-pass@example.org/private/HTTP_PATH_CANARY"
                    "?access_token=HTTP_QUERY_CANARY#HTTP_FRAGMENT_CANARY"
                ),
                observed_at="2026-08-20T00:30:00+00:00",
                freshness=FreshnessState.CURRENT,
            ),
            ResearchEvidence(
                source_id="local-source-id",
                source_kind=SourceKind.LOCAL_FILE,
                locator=r"C:\Users\Private User\Secrets\LOCAL_PATH_CANARY.txt",
                observed_at="2026-08-20T00:31:00+00:00",
                freshness=FreshnessState.CURRENT,
            ),
        ),
    )
    hostile = ResearchResultSet(
        result_set_id=base.result_set_id,
        workspace_id=base.workspace_id,
        query=base.query,
        items=(hostile_item,),
        created_at=base.created_at,
    )

    report = ResearchCardService(ResearchReviewRepository(_store(tmp_path))).accessible_report(hostile)

    assert "Location: http-source" in report.text
    assert "Location: local-file" in report.text
    for canary in (
        "resolver-user",
        "resolver-pass",
        "HTTP_PATH_CANARY",
        "HTTP_QUERY_CANARY",
        "HTTP_FRAGMENT_CANARY",
        "Private User",
        "LOCAL_PATH_CANARY",
    ):
        assert canary not in report.text


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

def test_accessible_report_rejects_behavioral_result_text_before_render(
    tmp_path: Path,
) -> None:
    class BehavioralTitle(str):
        def __format__(self, format_spec: str) -> str:
            return "FORGED_TITLE"

    base = _result_set()
    item = base.items[0]
    hostile_item = ResearchResultItem(
        ordinal=item.ordinal,
        document_id=item.document_id,
        title=BehavioralTitle(item.title),
        snippet=item.snippet,
        rank=item.rank,
        why_matched=item.why_matched,
        evidence=item.evidence,
    )
    hostile = ResearchResultSet(
        result_set_id=base.result_set_id,
        workspace_id=base.workspace_id,
        query=base.query,
        items=(hostile_item,),
        created_at=base.created_at,
    )

    service = ResearchCardService(ResearchReviewRepository(_store(tmp_path)))
    with pytest.raises(TypeError, match=r"result_set\.items\[0\]\.title must be an exact str"):
        service.accessible_report(hostile)


def test_accessible_report_rejects_behavioral_source_identity_before_hashing(
    tmp_path: Path,
) -> None:
    class BehavioralSourceId(str):
        def encode(self, *args: object, **kwargs: object) -> bytes:
            return b"forged-public-source"

    base = _result_set()
    item = base.items[0]
    evidence = item.evidence[0]
    hostile_evidence = ResearchEvidence(
        source_id=BehavioralSourceId(evidence.source_id),
        source_kind=evidence.source_kind,
        locator=evidence.locator,
        observed_at=evidence.observed_at,
        freshness=evidence.freshness,
    )
    hostile_item = ResearchResultItem(
        ordinal=item.ordinal,
        document_id=item.document_id,
        title=item.title,
        snippet=item.snippet,
        rank=item.rank,
        why_matched=item.why_matched,
        evidence=(hostile_evidence,),
    )
    hostile = ResearchResultSet(
        result_set_id=base.result_set_id,
        workspace_id=base.workspace_id,
        query=base.query,
        items=(hostile_item,),
        created_at=base.created_at,
    )

    service = ResearchCardService(ResearchReviewRepository(_store(tmp_path)))
    with pytest.raises(
        TypeError,
        match=r"result_set\.items\[0\]\.evidence\[0\]\.source_id must be an exact str",
    ):
        service.accessible_report(hostile)


def test_direct_text_renderer_rejects_forged_nested_report_carrier() -> None:
    base = ResearchReview(
        workspace_id="ws",
        document_id="doc-1",
        state=ResearchReviewState.SAVED,
    )
    card = ResearchCard(
        ordinal=0,
        document_id="doc-1",
        title="Title",
        snippet="Snippet",
        rank=float("nan"),
        why_matched="literal",
        evidence=(),
        review=base,
    )
    report = AccessibleResearchReport(
        result_set_id="results-1",
        workspace_id="ws",
        query="query",
        created_at="2026-08-20T01:00:00+00:00",
        cards=(card,),
        text="stale",
    )

    with pytest.raises(ValueError, match=r"report\.cards\[0\]\.rank must be finite"):
        render_accessible_report_text(report)

class _BehavioralReviewText(str):
    def strip(self, *_args: object, **_kwargs: object) -> str:
        raise AssertionError("review text behavior must not run before exact-type rejection")


@pytest.mark.parametrize(
    ("field_name", "value", "message"),
    (
        ("workspace_id", _BehavioralReviewText("ws"), "workspace_id must be an exact str"),
        ("document_id", _BehavioralReviewText("doc-1"), "document_id must be an exact str"),
        ("note", _BehavioralReviewText("safe"), "note must be an exact str"),
    ),
)
def test_review_write_rejects_behavioral_text_before_audit_effect(
    tmp_path: Path,
    field_name: str,
    value: object,
    message: str,
) -> None:
    store = _store(tmp_path)
    repository = ResearchReviewRepository(store)
    kwargs: dict[str, object] = {
        "workspace_id": "ws",
        "document_id": "doc-1",
        "state": ResearchReviewState.SAVED,
        "note": "safe",
    }
    kwargs[field_name] = value

    with pytest.raises(TypeError, match=message):
        repository.set_review(**kwargs)  # type: ignore[arg-type]

    with store.connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS event_count FROM audit_events "
            "WHERE event_type='research.review.changed'"
        ).fetchone()
    assert row is not None
    assert row["event_count"] == 0


@pytest.mark.parametrize(
    "mutation",
    (
        {"note": ["forged"]},
        {"note": 7},
        {"previous_state": "forged"},
        {"unexpected": "field"},
    ),
)
def test_review_readback_rejects_malformed_durable_audit_payload(
    tmp_path: Path,
    mutation: dict[str, object],
) -> None:
    store = _store(tmp_path)
    repository = ResearchReviewRepository(store)
    repository.set_review(
        workspace_id="ws",
        document_id="doc-1",
        state=ResearchReviewState.SAVED,
        note="safe",
    )

    with store.connection() as conn:
        row = conn.execute(
            "SELECT event_id, payload_json FROM audit_events "
            "WHERE event_type='research.review.changed' ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        assert row is not None
        payload = json.loads(row["payload_json"])
        payload.update(mutation)
        conn.execute(
            "UPDATE audit_events SET payload_json=? WHERE event_id=?",
            (json.dumps(payload, ensure_ascii=False, sort_keys=True), row["event_id"]),
        )

    restarted = ResearchReviewRepository(SQLiteStore(store.path))
    with pytest.raises(RuntimeError, match="review audit evidence is invalid"):
        restarted.get_review(workspace_id="ws", document_id="doc-1")


def test_review_readback_rejects_duplicate_durable_audit_authority_key(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    repository = ResearchReviewRepository(store)
    repository.set_review(
        workspace_id="ws",
        document_id="doc-1",
        state=ResearchReviewState.SAVED,
        note="safe",
    )

    with store.connection() as conn:
        row = conn.execute(
            """SELECT event_id, payload_json FROM audit_events
            WHERE event_type='research.review.changed'
            ORDER BY event_id DESC LIMIT 1"""
        ).fetchone()
        assert row is not None
        duplicate_payload = row["payload_json"].replace(
            '"state": "saved"',
            '"state": "saved", "state": "dismissed"',
        )
        assert duplicate_payload != row["payload_json"]
        assert json.loads(duplicate_payload)["state"] == "dismissed"
        conn.execute(
            "UPDATE audit_events SET payload_json=? WHERE event_id=?",
            (duplicate_payload, row["event_id"]),
        )

    restarted = ResearchReviewRepository(SQLiteStore(store.path))
    with pytest.raises(RuntimeError, match="review audit evidence is invalid"):
        restarted.get_review(workspace_id="ws", document_id="doc-1")


def test_review_readback_rejects_valid_but_inconsistent_predecessor_state(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    repository = ResearchReviewRepository(store)
    repository.set_review(
        workspace_id="ws",
        document_id="doc-1",
        state=ResearchReviewState.SAVED,
        note="saved",
    )
    repository.set_review(
        workspace_id="ws",
        document_id="doc-1",
        state=ResearchReviewState.DISMISSED,
        note="dismissed",
    )

    with store.connection() as conn:
        row = conn.execute(
            """SELECT event_id, payload_json FROM audit_events
            WHERE event_type='research.review.changed'
            ORDER BY event_id DESC LIMIT 1"""
        ).fetchone()
        assert row is not None
        payload = json.loads(row["payload_json"])
        payload["previous_state"] = ResearchReviewState.UNREVIEWED.value
        conn.execute(
            "UPDATE audit_events SET payload_json=? WHERE event_id=?",
            (json.dumps(payload, ensure_ascii=False, sort_keys=True), row["event_id"]),
        )

    restarted = ResearchReviewRepository(SQLiteStore(store.path))
    with pytest.raises(RuntimeError, match="review audit chain is inconsistent"):
        restarted.get_review(workspace_id="ws", document_id="doc-1")

def test_review_readback_rejects_noncanonical_durable_timestamp(tmp_path: Path) -> None:
    store = _store(tmp_path)
    repository = ResearchReviewRepository(store)
    repository.set_review(
        workspace_id="ws",
        document_id="doc-1",
        state=ResearchReviewState.SAVED,
        note="safe",
    )

    with store.connection() as conn:
        conn.execute(
            "UPDATE audit_events SET created_at=? "
            "WHERE event_type='research.review.changed'",
            ("2026-08-20T00:00:00",),
        )

    restarted = ResearchReviewRepository(SQLiteStore(store.path))
    with pytest.raises(RuntimeError, match="review audit timestamp is invalid"):
        restarted.get_review(workspace_id="ws", document_id="doc-1")

