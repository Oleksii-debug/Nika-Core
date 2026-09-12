from __future__ import annotations

from nika_core.learning_cognition import (
    CognitionCandidate,
    CognitionCandidateKind,
    CognitionEvidenceRef,
)
from nika_core.learning_comparison import ExperienceMemoryComparison

_MAX_COMPARISONS = 64
_COMPARISON_SOURCE_TYPE = "experience_memory_comparison"


def _canonical_comparisons(
    value: object,
) -> tuple[ExperienceMemoryComparison, ...]:
    if type(value) is not tuple:
        raise TypeError("comparisons must be an immutable tuple")
    if not 1 <= len(value) <= _MAX_COMPARISONS:
        raise ValueError("comparisons count is outside the supported bound")
    if any(type(item) is not ExperienceMemoryComparison for item in value):
        raise TypeError("comparisons must contain ExperienceMemoryComparison values")

    first = value[0]
    workspace_id = first.workspace_id
    agent_id = first.agent_id
    if any(item.workspace_id != workspace_id for item in value):
        raise ValueError("all comparisons must belong to the same workspace")
    if any(item.agent_id != agent_id for item in value):
        raise ValueError("all comparisons must belong to the same agent")

    comparison_ids = tuple(item.comparison_id for item in value)
    if len(set(comparison_ids)) != len(comparison_ids):
        raise ValueError("duplicate comparison identities are not allowed")
    return value


def cognition_evidence_from_comparison(
    comparison: ExperienceMemoryComparison,
) -> CognitionEvidenceRef:
    if type(comparison) is not ExperienceMemoryComparison:
        raise TypeError("comparison must be an ExperienceMemoryComparison")
    return CognitionEvidenceRef(
        source_type=_COMPARISON_SOURCE_TYPE,
        source_id=comparison.comparison_id,
        evidence_sha256=comparison.comparison_sha256,
    )


def cognition_candidate_from_comparisons(
    *,
    candidate_id: str,
    kind: CognitionCandidateKind,
    statement: str,
    comparisons: tuple[ExperienceMemoryComparison, ...],
) -> CognitionCandidate:
    canonical = _canonical_comparisons(comparisons)
    evidence = tuple(cognition_evidence_from_comparison(item) for item in canonical)
    first = canonical[0]
    return CognitionCandidate.create(
        candidate_id=candidate_id,
        workspace_id=first.workspace_id,
        agent_id=first.agent_id,
        kind=kind,
        statement=statement,
        evidence=evidence,
    )
