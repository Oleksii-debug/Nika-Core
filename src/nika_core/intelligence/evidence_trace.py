from __future__ import annotations

from dataclasses import dataclass

from nika_core.research.models import FreshnessState, ResearchResultSet, SourceKind

_TRACE_SCHEMA = "nika.intelligence.research-evidence-trace:v1"
_PROVENANCE_SCHEMA = "nika.intelligence.research-provenance:v1"


class ResearchEvidenceTraceError(ValueError):
    """Raised when selected research evidence cannot be proven from retrieval output."""


@dataclass(frozen=True, slots=True)
class ResearchEvidenceSelection:
    """Exact caller selection coordinates within one canonical retrieval result set."""

    result_set_id: str
    item_ordinal: int
    document_id: str
    evidence_index: int
    source_id: str
    observed_at: str

    def __post_init__(self) -> None:
        for label, value in (
            ("result_set_id", self.result_set_id),
            ("document_id", self.document_id),
            ("source_id", self.source_id),
            ("observed_at", self.observed_at),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label} must not be empty")
        if isinstance(self.item_ordinal, bool) or not isinstance(self.item_ordinal, int):
            raise TypeError("item_ordinal must be an integer")
        if self.item_ordinal < 0:
            raise ValueError("item_ordinal must be non-negative")
        if isinstance(self.evidence_index, bool) or not isinstance(self.evidence_index, int):
            raise TypeError("evidence_index must be an integer")
        if self.evidence_index < 0:
            raise ValueError("evidence_index must be non-negative")


@dataclass(frozen=True, slots=True)
class ResearchEvidenceTrace:
    """Compact retrieval identity safe to carry with a response or plan."""

    result_set_id: str
    workspace_id: str
    result_created_at: str
    item_ordinal: int
    document_id: str
    evidence_index: int
    source_id: str
    source_kind: SourceKind
    observed_at: str
    freshness: FreshnessState | None

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": _TRACE_SCHEMA,
            "result_set_id": self.result_set_id,
            "workspace_id": self.workspace_id,
            "result_created_at": self.result_created_at,
            "item_ordinal": self.item_ordinal,
            "document_id": self.document_id,
            "evidence_index": self.evidence_index,
            "source_id": self.source_id,
            "source_kind": self.source_kind.value,
            "observed_at": self.observed_at,
            "freshness": self.freshness.value if self.freshness is not None else None,
        }


@dataclass(frozen=True, slots=True)
class ResearchAssistedProvenance:
    """Machine-readable evidence provenance for a research-assisted output."""

    research_evidence: tuple[ResearchEvidenceTrace, ...]

    def __post_init__(self) -> None:
        if not self.research_evidence:
            raise ValueError("research-assisted provenance requires selected evidence")

    def to_payload(self) -> dict[str, object]:
        return {
            "schema": _PROVENANCE_SCHEMA,
            "research_evidence": [item.to_payload() for item in self.research_evidence],
        }


def trace_selected_research_evidence(
    *,
    result_set: ResearchResultSet,
    selection: ResearchEvidenceSelection,
) -> ResearchEvidenceTrace:
    """Prove one exact selection against its retrieval result before emitting provenance."""
    if not isinstance(result_set, ResearchResultSet):
        raise TypeError("result_set must be a ResearchResultSet")
    if result_set.result_set_id != selection.result_set_id:
        raise ResearchEvidenceTraceError("selection result_set_id does not match retrieval result")

    matching_items = [item for item in result_set.items if item.ordinal == selection.item_ordinal]
    if len(matching_items) != 1:
        raise ResearchEvidenceTraceError("selected item ordinal is absent or ambiguous")
    item = matching_items[0]
    if item.document_id != selection.document_id:
        raise ResearchEvidenceTraceError("selected document_id does not match retrieval item")
    if selection.evidence_index >= len(item.evidence):
        raise ResearchEvidenceTraceError("selected evidence index is outside retrieval item")

    evidence = item.evidence[selection.evidence_index]
    if evidence.source_id != selection.source_id:
        raise ResearchEvidenceTraceError("selected source_id does not match retrieval evidence")
    if evidence.observed_at != selection.observed_at:
        raise ResearchEvidenceTraceError("selected observed_at does not match retrieval evidence")

    return ResearchEvidenceTrace(
        result_set_id=result_set.result_set_id,
        workspace_id=result_set.workspace_id,
        result_created_at=result_set.created_at,
        item_ordinal=item.ordinal,
        document_id=item.document_id,
        evidence_index=selection.evidence_index,
        source_id=evidence.source_id,
        source_kind=evidence.source_kind,
        observed_at=evidence.observed_at,
        freshness=evidence.freshness,
    )


def build_research_assisted_provenance(
    *,
    result_set: ResearchResultSet,
    selections: tuple[ResearchEvidenceSelection, ...],
) -> ResearchAssistedProvenance:
    """Build output provenance only from selections proven against canonical retrieval data."""
    if not selections:
        raise ValueError("at least one selected research evidence item is required")
    return ResearchAssistedProvenance(
        research_evidence=tuple(
            trace_selected_research_evidence(result_set=result_set, selection=selection)
            for selection in selections
        )
    )
