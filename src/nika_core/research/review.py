from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from nika_core.data.sqlite import SQLiteStore
from nika_core.research.models import ResearchEvidence, ResearchResultSet


_MAX_NOTE_LENGTH = 4000
_EVENT_TYPE = "research.review.changed"
_ENTITY_TYPE = "research_document_review"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _required(value: str, field_name: str) -> str:
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
        if not isinstance(state, ResearchReviewState):
            raise TypeError("state must be a ResearchReviewState")
        if len(note) > _MAX_NOTE_LENGTH:
            raise ValueError(f"note exceeds {_MAX_NOTE_LENGTH} characters")

        current = self.get_review(workspace_id=workspace, document_id=document)
        normalized_note = note.strip()
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
        with self._store.connection() as conn:
            conn.execute(
                """INSERT INTO audit_events(
                    event_type, entity_type, entity_id, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?)""",
                (_EVENT_TYPE, _ENTITY_TYPE, _entity_id(workspace, document), payload, created_at),
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
        self._require_document(workspace_id=workspace, document_id=document)
        with self._store.connection() as conn:
            row = conn.execute(
                """SELECT payload_json, created_at FROM audit_events
                WHERE event_type=? AND entity_type=? AND entity_id=?
                ORDER BY event_id DESC LIMIT 1""",
                (_EVENT_TYPE, _ENTITY_TYPE, _entity_id(workspace, document)),
            ).fetchone()
        if row is None:
            return ResearchReview(
                workspace_id=workspace,
                document_id=document,
                state=ResearchReviewState.UNREVIEWED,
            )
        payload = json.loads(row["payload_json"])
        if payload.get("workspace_id") != workspace or payload.get("document_id") != document:
            raise RuntimeError("research review audit identity mismatch")
        return ResearchReview(
            workspace_id=workspace,
            document_id=document,
            state=ResearchReviewState(payload["state"]),
            note=str(payload.get("note", "")),
            updated_at=row["created_at"],
        )

    def _require_document(self, *, workspace_id: str, document_id: str) -> None:
        with self._store.connection() as conn:
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

    def cards_for(self, result_set: ResearchResultSet) -> tuple[ResearchCard, ...]:
        cards: list[ResearchCard] = []
        for item in result_set.items:
            cards.append(
                ResearchCard(
                    ordinal=item.ordinal,
                    document_id=item.document_id,
                    title=item.title,
                    snippet=item.snippet,
                    rank=item.rank,
                    why_matched=item.why_matched,
                    evidence=item.evidence,
                    review=self._reviews.get_review(
                        workspace_id=result_set.workspace_id,
                        document_id=item.document_id,
                    ),
                )
            )
        return tuple(cards)

    def accessible_report(self, result_set: ResearchResultSet) -> AccessibleResearchReport:
        cards = self.cards_for(result_set)
        lines = [
            "Research results",
            f"Query: {result_set.query}",
            f"Created: {result_set.created_at}",
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
                            f"  Source ID: {evidence.source_id}",
                            f"  Source kind: {evidence.source_kind.value}",
                            f"  Freshness: {freshness}",
                            f"  Location: {evidence.locator}",
                            f"  Observed: {evidence.observed_at}",
                        ]
                    )
            else:
                lines.append("Evidence: none recorded")
            lines.append("")

        return AccessibleResearchReport(
            result_set_id=result_set.result_set_id,
            workspace_id=result_set.workspace_id,
            query=result_set.query,
            created_at=result_set.created_at,
            cards=cards,
            text="\n".join(lines).rstrip() + "\n",
        )
