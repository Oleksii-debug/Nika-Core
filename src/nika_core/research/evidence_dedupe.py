from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from nika_core.data.sqlite import SQLiteStore
from nika_core.research.models import ResearchEvidence, SearchHit, SourceKind


@dataclass(frozen=True, slots=True)
class ResearchEvidenceIdentity:
    """Stable identity for one source-backed item revision in a result set."""

    source_kind: str
    source_id: str
    revision_id: str


class ResearchEvidenceDeduplicator:
    """Collapse repeated evidence without collapsing distinct sources or revisions."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def identity(
        self,
        document_id: str,
        evidence: ResearchEvidence,
    ) -> ResearchEvidenceIdentity:
        if not document_id.strip():
            raise ValueError("document_id is required for evidence identity")
        if evidence.source_kind is SourceKind.HTTP:
            revision_id = self._http_revision_id(document_id, evidence)
        else:
            # Legacy local corpus identity is content-addressed at the document layer.
            revision_id = f"document:{document_id}"
        return ResearchEvidenceIdentity(
            source_kind=evidence.source_kind.value,
            source_id=evidence.source_id,
            revision_id=revision_id,
        )

    def deduplicate(
        self,
        document_id: str,
        evidence: Iterable[ResearchEvidence],
    ) -> tuple[ResearchEvidence, ...]:
        selected: dict[ResearchEvidenceIdentity, ResearchEvidence] = {}
        for item in evidence:
            identity = self.identity(document_id, item)
            previous = selected.get(identity)
            if previous is None or self._evidence_preference(item) > self._evidence_preference(
                previous
            ):
                selected[identity] = item
        return tuple(
            sorted(
                selected.values(),
                key=lambda item: (
                    item.observed_at,
                    item.source_kind.value,
                    item.source_id,
                    item.locator,
                    item.freshness.value if item.freshness is not None else "",
                ),
            )
        )

    def _http_revision_id(self, document_id: str, evidence: ResearchEvidence) -> str:
        with self._store.connection() as conn:
            rows = conn.execute(
                """SELECT snapshot_id
                FROM corpus_http_origins
                WHERE document_id=? AND source_id=? AND locator=? AND observed_at=?
                ORDER BY snapshot_id""",
                (
                    document_id,
                    evidence.source_id,
                    evidence.locator,
                    evidence.observed_at,
                ),
            ).fetchall()
        if len(rows) == 1:
            # snapshot_id is already the canonical source_id + raw revision identity.
            return f"snapshot:{rows[0]['snapshot_id']}"
        if len(rows) > 1:
            raise ValueError(
                "ambiguous HTTP revision provenance cannot be deduplicated safely"
            )

        # No durable HTTP origin could be resolved. Preserve the occurrence identity rather
        # than inventing a revision authority from incomplete provenance.
        return (
            f"occurrence:{document_id}\0{evidence.locator}\0{evidence.observed_at}"
        )

    @staticmethod
    def _evidence_preference(evidence: ResearchEvidence) -> tuple[str, str, str]:
        return (
            evidence.observed_at,
            evidence.locator,
            evidence.freshness.value if evidence.freshness is not None else "",
        )


def deduplicate_search_hits(hits: Iterable[SearchHit]) -> tuple[SearchHit, ...]:
    """Keep one non-boosted ranking observation for each corpus item revision."""

    selected: dict[str, SearchHit] = {}
    for hit in hits:
        previous = selected.get(hit.document_id)
        if previous is None or _hit_preference(hit) < _hit_preference(previous):
            selected[hit.document_id] = hit
    return tuple(sorted(selected.values(), key=_hit_preference))


def _hit_preference(hit: SearchHit) -> tuple[float, str, str, str]:
    # Existing FTS5 SearchHit semantics use numerically lower BM25 ranks as better matches.
    return (hit.rank, hit.document_id, hit.title, hit.snippet)
