from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from nika_core.data.sqlite import SQLiteStore
from nika_core.research.models import (
    FreshnessState,
    ResearchEvidence,
    ResearchResultItem,
    ResearchResultSet,
    SourceKind,
)

_MAX_NOTE_LENGTH = 4000
_EVENT_TYPE = "research.review.changed"
_ENTITY_TYPE = "research_document_review"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _required(value: str, field_name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be an exact str")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _entity_id(workspace_id: str, document_id: str) -> str:
    payload = f"{workspace_id}\0{document_id}".encode()
    return hashlib.sha256(payload).hexdigest()


class ResearchReviewState(StrEnum):
    UNREVIEWED = "unreviewed"
    SAVED = "saved"
    DISMISSED = "dismissed"


@dataclass(frozen=True, slots=True)
class ResearchReview:
    workspace_id: str
    document_id: str
    state: ResearchReviewState
    note: str = ""
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class ResearchCard:
    ordinal: int
    document_id: str
    title: str
    snippet: str
    rank: float
    why_matched: str
    evidence: tuple[ResearchEvidence, ...]
    review: ResearchReview


@dataclass(frozen=True, slots=True)
class AccessibleResearchReport:
    result_set_id: str
    workspace_id: str
    query: str
    created_at: str
    cards: tuple[ResearchCard, ...]
    text: str


def _exact_text(value: object, field_name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be an exact str")
    return value


def _canonical_review_timestamp(value: object) -> str:
    if type(value) is not str:
        raise RuntimeError("research review audit timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise RuntimeError("research review audit timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError("research review audit timestamp is invalid")
    if parsed.astimezone(UTC).isoformat() != value:
        raise RuntimeError("research review audit timestamp is invalid")
    return value


def _strict_json_object_pairs(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _decode_review_audit_payload(
    raw_payload: object,
    *,
    workspace_id: str,
    document_id: str,
) -> tuple[ResearchReviewState, ResearchReviewState, str]:
    if type(raw_payload) is not str:
        raise RuntimeError("research review audit evidence is invalid")
    try:
        payload = json.loads(raw_payload, object_pairs_hook=_strict_json_object_pairs)
        expected_keys = {
            "workspace_id",
            "document_id",
            "previous_state",
            "state",
            "note",
        }
        if type(payload) is not dict or set(payload) != expected_keys:
            raise ValueError("unexpected review audit payload shape")
        stored_workspace = _exact_text(payload["workspace_id"], "audit workspace_id")
        stored_document = _exact_text(payload["document_id"], "audit document_id")
        previous_state = ResearchReviewState(
            _exact_text(payload["previous_state"], "audit previous_state")
        )
        state = ResearchReviewState(_exact_text(payload["state"], "audit state"))
        note = _exact_text(payload["note"], "audit note")
        if len(note) > _MAX_NOTE_LENGTH or note.strip() != note:
            raise ValueError("review audit note is not canonical")
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("research review audit evidence is invalid") from exc
    if stored_workspace != workspace_id or stored_document != document_id:
        raise RuntimeError("research review audit identity mismatch")
    return previous_state, state, note


def _canonical_evidence(evidence: object, field_name: str) -> ResearchEvidence:
    if type(evidence) is not ResearchEvidence:
        raise TypeError(f"{field_name} must be an exact ResearchEvidence")
    if type(evidence.source_kind) is not SourceKind:
        raise TypeError(f"{field_name}.source_kind must be an exact SourceKind")
    if evidence.freshness is not None and type(evidence.freshness) is not FreshnessState:
        raise TypeError(f"{field_name}.freshness must be an exact FreshnessState or None")
    return ResearchEvidence(
        source_id=_exact_text(evidence.source_id, f"{field_name}.source_id"),
        source_kind=evidence.source_kind,
        locator=_exact_text(evidence.locator, f"{field_name}.locator"),
        observed_at=_exact_text(evidence.observed_at, f"{field_name}.observed_at"),
        freshness=evidence.freshness,
    )


def _canonical_review(review: object, field_name: str) -> ResearchReview:
    if type(review) is not ResearchReview:
        raise TypeError(f"{field_name} must be an exact ResearchReview")
    if type(review.state) is not ResearchReviewState:
        raise TypeError(f"{field_name}.state must be an exact ResearchReviewState")
    updated_at = review.updated_at
    if updated_at is not None:
        updated_at = _exact_text(updated_at, f"{field_name}.updated_at")
    return ResearchReview(
        workspace_id=_exact_text(review.workspace_id, f"{field_name}.workspace_id"),
        document_id=_exact_text(review.document_id, f"{field_name}.document_id"),
        state=review.state,
        note=_exact_text(review.note, f"{field_name}.note"),
        updated_at=updated_at,
    )


def _canonical_rank(value: object, field_name: str) -> float:
    if type(value) is not float:
        raise TypeError(f"{field_name} must be an exact float")
    if not math.isfinite(value):
        raise ValueError(f"{field_name} must be finite")
    return value


def _canonical_evidence_tuple(value: object, field_name: str) -> tuple[ResearchEvidence, ...]:
    if type(value) is not tuple:
        raise TypeError(f"{field_name} must be an exact tuple")
    return tuple(
        _canonical_evidence(item, f"{field_name}[{index}]")
        for index, item in enumerate(value)
    )


def _canonical_card(card: object, field_name: str) -> ResearchCard:
    if type(card) is not ResearchCard:
        raise TypeError(f"{field_name} must be an exact ResearchCard")
    if type(card.ordinal) is not int:
        raise TypeError(f"{field_name}.ordinal must be an exact int")
    if card.ordinal < 0:
        raise ValueError(f"{field_name}.ordinal must be non-negative")
    return ResearchCard(
        ordinal=card.ordinal,
        document_id=_exact_text(card.document_id, f"{field_name}.document_id"),
        title=_exact_text(card.title, f"{field_name}.title"),
        snippet=_exact_text(card.snippet, f"{field_name}.snippet"),
        rank=_canonical_rank(card.rank, f"{field_name}.rank"),
        why_matched=_exact_text(card.why_matched, f"{field_name}.why_matched"),
        evidence=_canonical_evidence_tuple(card.evidence, f"{field_name}.evidence"),
        review=_canonical_review(card.review, f"{field_name}.review"),
    )


def canonical_accessible_report(report: object) -> AccessibleResearchReport:
    """Reconstruct one public report into detached canonical runtime carriers."""

    if type(report) is not AccessibleResearchReport:
        raise TypeError("report must be an exact AccessibleResearchReport")
    if type(report.cards) is not tuple:
        raise TypeError("report.cards must be an exact tuple")
    cards = tuple(
        _canonical_card(card, f"report.cards[{index}]")
        for index, card in enumerate(report.cards)
    )
    workspace_id = _exact_text(report.workspace_id, "report.workspace_id")
    for index, card in enumerate(cards):
        if card.review.workspace_id != workspace_id:
            raise ValueError(f"report.cards[{index}].review workspace mismatch")
        if card.review.document_id != card.document_id:
            raise ValueError(f"report.cards[{index}].review document mismatch")
    return AccessibleResearchReport(
        result_set_id=_exact_text(report.result_set_id, "report.result_set_id"),
        workspace_id=workspace_id,
        query=_exact_text(report.query, "report.query"),
        created_at=_exact_text(report.created_at, "report.created_at"),
        cards=cards,
        text=_exact_text(report.text, "report.text"),
    )


def _canonical_result_item(item: object, field_name: str) -> ResearchResultItem:
    if type(item) is not ResearchResultItem:
        raise TypeError(f"{field_name} must be an exact ResearchResultItem")
    if type(item.ordinal) is not int:
        raise TypeError(f"{field_name}.ordinal must be an exact int")
    if item.ordinal < 0:
        raise ValueError(f"{field_name}.ordinal must be non-negative")
    return ResearchResultItem(
        ordinal=item.ordinal,
        document_id=_exact_text(item.document_id, f"{field_name}.document_id"),
        title=_exact_text(item.title, f"{field_name}.title"),
        snippet=_exact_text(item.snippet, f"{field_name}.snippet"),
        rank=_canonical_rank(item.rank, f"{field_name}.rank"),
        why_matched=_exact_text(item.why_matched, f"{field_name}.why_matched"),
        evidence=_canonical_evidence_tuple(item.evidence, f"{field_name}.evidence"),
    )


def _canonical_result_set(result_set: object) -> ResearchResultSet:
    if type(result_set) is not ResearchResultSet:
        raise TypeError("result_set must be an exact ResearchResultSet")
    if type(result_set.items) is not tuple:
        raise TypeError("result_set.items must be an exact tuple")
    items = tuple(
        _canonical_result_item(item, f"result_set.items[{index}]")
        for index, item in enumerate(result_set.items)
    )
    return ResearchResultSet(
        result_set_id=_exact_text(result_set.result_set_id, "result_set.result_set_id"),
        workspace_id=_exact_text(result_set.workspace_id, "result_set.workspace_id"),
        query=_exact_text(result_set.query, "result_set.query"),
        items=items,
        created_at=_exact_text(result_set.created_at, "result_set.created_at"),
    )


def safe_evidence_source_reference(evidence: ResearchEvidence) -> str:
    """Return a bounded deterministic public token for an internal source identity.

    ``ResearchEvidence.source_id`` is internal exact-correlation state and may be
    caller supplied. Public reports therefore never reproduce it verbatim. The
    SHA-256 token is stable across report formats while revealing no raw URL,
    path, credential, or token bytes from the internal identifier.
    """

    evidence = _canonical_evidence(evidence, "evidence")
    digest = hashlib.sha256(evidence.source_id.encode("utf-8")).hexdigest()
    return f"source-sha256:{digest}"


def safe_evidence_locator(evidence: ResearchEvidence) -> str:
    """Return a public provenance label without exposing the raw source locator.

    Exact provenance remains on the internal evidence record. Public reports pair
    this coarse locator label with ``safe_evidence_source_reference`` rather than
    reproducing raw source IDs, URLs, signed paths, credentials, query strings,
    fragments, or private local filesystem paths.
    """

    evidence = _canonical_evidence(evidence, "evidence")
    if evidence.source_kind is SourceKind.HTTP:
        return "http-source"
    if evidence.source_kind is SourceKind.LOCAL_FILE:
        return "local-file"
    raise ValueError("unsupported research evidence source kind")


def _render_accessible_report_text(
    *,
    query: str,
    created_at: str,
    cards: tuple[ResearchCard, ...],
) -> str:
    lines = [
        "Research results",
        f"Query: {query}",
        f"Created: {created_at}",
        f"Results: {len(cards)}",
        "",
    ]
    for position, card in enumerate(cards, start=1):
        lines.extend(
            [
                f"Result {position}: {card.title}",
                f"Review: {card.review.state.value}",
                f"Rank: {card.rank}",
                f"Why matched: {card.why_matched}",
                f"Summary: {card.snippet}",
            ]
        )
        if card.review.note:
            lines.append(f"Review note: {card.review.note}")
        if card.evidence:
            lines.append("Evidence:")
            for evidence_index, evidence in enumerate(card.evidence, start=1):
                freshness = evidence.freshness.value if evidence.freshness is not None else "n/a"
                lines.extend(
                    [
                        f"  Evidence {evidence_index}",
                        f"  Source ID: {safe_evidence_source_reference(evidence)}",
                        f"  Source kind: {evidence.source_kind.value}",
                        f"  Freshness: {freshness}",
                        f"  Location: {safe_evidence_locator(evidence)}",
                        f"  Observed: {evidence.observed_at}",
                    ]
                )
        else:
            lines.append("Evidence: none recorded")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_accessible_report_text(report: AccessibleResearchReport) -> str:
    """Render the canonical public plain-text representation from structured cards."""

    report = canonical_accessible_report(report)
    return _render_accessible_report_text(
        query=report.query,
        created_at=report.created_at,
        cards=report.cards,
    )


class ResearchReviewRepository:
    """Durable review state recorded in Nika's existing authoritative audit log.

    Review is intentionally keyed by workspace + normalized corpus document identity,
    so the user's decision survives result-set reruns without creating a second
    scheduler, database, or review-state kernel. Every state change remains an
    append-only audit event; the latest event is the current projection.
    """

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def set_review(
        self,
        *,
        workspace_id: str,
        document_id: str,
        state: ResearchReviewState,
        note: str = "",
    ) -> ResearchReview:
        workspace = _required(workspace_id, "workspace_id")
        document = _required(document_id, "document_id")
        if type(state) is not ResearchReviewState:
            raise TypeError("state must be an exact ResearchReviewState")
        if type(note) is not str:
            raise TypeError("note must be an exact str")
        if len(note) > _MAX_NOTE_LENGTH:
            raise ValueError(f"note exceeds {_MAX_NOTE_LENGTH} characters")
        normalized_note = note.strip()

        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._require_document_on_connection(
                conn,
                workspace_id=workspace,
                document_id=document,
            )
            current = self._get_review_on_connection(
                conn,
                workspace_id=workspace,
                document_id=document,
            )
            if current.state is state and current.note == normalized_note:
                return current

            created_at = _now()
            payload = json.dumps(
                {
                    "workspace_id": workspace,
                    "document_id": document,
                    "previous_state": current.state.value,
                    "state": state.value,
                    "note": normalized_note,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            conn.execute(
                """INSERT INTO audit_events(
                    event_type, entity_type, entity_id, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)""",
                (
                    _EVENT_TYPE,
                    _ENTITY_TYPE,
                    _entity_id(workspace, document),
                    payload,
                    created_at,
                ),
            )
        return ResearchReview(
            workspace_id=workspace,
            document_id=document,
            state=state,
            note=normalized_note,
            updated_at=created_at,
        )

    def get_review(self, *, workspace_id: str, document_id: str) -> ResearchReview:
        workspace = _required(workspace_id, "workspace_id")
        document = _required(document_id, "document_id")
        with self._store.connection() as conn:
            self._require_document_on_connection(
                conn,
                workspace_id=workspace,
                document_id=document,
            )
            return self._get_review_on_connection(
                conn,
                workspace_id=workspace,
                document_id=document,
            )

    def _get_review_on_connection(
        self,
        conn: sqlite3.Connection,
        *,
        workspace_id: str,
        document_id: str,
    ) -> ResearchReview:
        rows = conn.execute(
            """SELECT payload_json, created_at FROM audit_events
            WHERE event_type=? AND entity_type=? AND entity_id=?
            ORDER BY event_id""",
            (_EVENT_TYPE, _ENTITY_TYPE, _entity_id(workspace_id, document_id)),
        ).fetchall()
        if not rows:
            return ResearchReview(
                workspace_id=workspace_id,
                document_id=document_id,
                state=ResearchReviewState.UNREVIEWED,
            )

        expected_previous_state = ResearchReviewState.UNREVIEWED
        current: ResearchReview | None = None
        for row in rows:
            previous_state, state, note = _decode_review_audit_payload(
                row["payload_json"],
                workspace_id=workspace_id,
                document_id=document_id,
            )
            if previous_state is not expected_previous_state:
                raise RuntimeError("research review audit chain is inconsistent")
            updated_at = _canonical_review_timestamp(row["created_at"])
            current = ResearchReview(
                workspace_id=workspace_id,
                document_id=document_id,
                state=state,
                note=note,
                updated_at=updated_at,
            )
            expected_previous_state = state
        if current is None:
            raise RuntimeError("research review audit chain is invalid")
        return current

    @staticmethod
    def _require_document_on_connection(
        conn: sqlite3.Connection,
        *,
        workspace_id: str,
        document_id: str,
    ) -> None:
        row = conn.execute(
            "SELECT workspace_id FROM corpus_documents WHERE document_id=?",
            (document_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown corpus document: {document_id}")
        if row["workspace_id"] != workspace_id:
            raise ValueError("document does not belong to the requested workspace")


class ResearchCardService:
    def __init__(self, reviews: ResearchReviewRepository) -> None:
        self._reviews = reviews

    def _cards_for_canonical(self, result_set: ResearchResultSet) -> tuple[ResearchCard, ...]:
        cards: list[ResearchCard] = []
        for index, item in enumerate(result_set.items):
            review = _canonical_review(
                self._reviews.get_review(
                    workspace_id=result_set.workspace_id,
                    document_id=item.document_id,
                ),
                f"result_set.items[{index}].review",
            )
            cards.append(
                ResearchCard(
                    ordinal=item.ordinal,
                    document_id=item.document_id,
                    title=item.title,
                    snippet=item.snippet,
                    rank=item.rank,
                    why_matched=item.why_matched,
                    evidence=item.evidence,
                    review=review,
                )
            )
        return tuple(cards)

    def cards_for(self, result_set: ResearchResultSet) -> tuple[ResearchCard, ...]:
        canonical = _canonical_result_set(result_set)
        return self._cards_for_canonical(canonical)

    def accessible_report(self, result_set: ResearchResultSet) -> AccessibleResearchReport:
        canonical = _canonical_result_set(result_set)
        cards = self._cards_for_canonical(canonical)
        report = AccessibleResearchReport(
            result_set_id=canonical.result_set_id,
            workspace_id=canonical.workspace_id,
            query=canonical.query,
            created_at=canonical.created_at,
            cards=cards,
            text=_render_accessible_report_text(
                query=canonical.query,
                created_at=canonical.created_at,
                cards=cards,
            ),
        )
        return canonical_accessible_report(report)
