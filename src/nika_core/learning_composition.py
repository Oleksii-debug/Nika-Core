from __future__ import annotations

from nika_core.learning_cognition import (
    CognitionCandidate,
    CognitionCandidateKind,
    CognitionEvidenceRef,
)
from nika_core.learning_comparison import ExperienceMemoryComparison

_MAX_COMPARISONS = 64
_COMPARISON_SOURCE_TYPE = "experience_memory_comparison"


def _canonical_comparison(
    value: object,
    *,
    expected_comparator_sha256: str,
    expected_comparison_policy_sha256: str,
) -> ExperienceMemoryComparison:
    if type(value) is not ExperienceMemoryComparison:
        raise TypeError("comparison must be an ExperienceMemoryComparison")
    return ExperienceMemoryComparison.revalidate(
        value,
        expected_comparator_sha256=expected_comparator_sha256,
        expected_comparison_policy_sha256=expected_comparison_policy_sha256,
    )


def _canonical_comparisons(
    value: object,
    *,
    expected_comparator_sha256: str,
    expected_comparison_policy_sha256: str,
) -> tuple[ExperienceMemoryComparison, ...]:
    if type(value) is not tuple:
        raise TypeError("comparisons must be an immutable tuple")
    if not 1 <= len(value) <= _MAX_COMPARISONS:
        raise ValueError("comparisons count is outside the supported bound")
    if any(type(item) is not ExperienceMemoryComparison for item in value):
        raise TypeError("comparisons must contain ExperienceMemoryComparison values")

    canonical = tuple(
        _canonical_comparison(
            item,
            expected_comparator_sha256=expected_comparator_sha256,
            expected_comparison_policy_sha256=expected_comparison_policy_sha256,
        )
        for item in value
    )

    first = canonical[0]
    workspace_id = first.workspace_id
    agent_id = first.agent_id
    if any(item.workspace_id != workspace_id for item in canonical):
        raise ValueError("all comparisons must belong to the same workspace")
    if any(item.agent_id != agent_id for item in canonical):
        raise ValueError("all comparisons must belong to the same agent")

    comparison_ids = tuple(item.comparison_id for item in canonical)
    if len(set(comparison_ids)) != len(comparison_ids):
        raise ValueError("duplicate comparison identities are not allowed")
    return canonical


def _cognition_evidence_from_canonical_comparison(
    comparison: ExperienceMemoryComparison,
) -> CognitionEvidenceRef:
    return CognitionEvidenceRef(
        source_type=_COMPARISON_SOURCE_TYPE,
        source_id=comparison.comparison_id,
        evidence_sha256=comparison.comparison_sha256,
    )


def cognition_evidence_from_comparison(
    comparison: ExperienceMemoryComparison,
    *,
    expected_comparator_sha256: str,
    expected_comparison_policy_sha256: str,
) -> CognitionEvidenceRef:
    canonical = _canonical_comparison(
        comparison,
        expected_comparator_sha256=expected_comparator_sha256,
        expected_comparison_policy_sha256=expected_comparison_policy_sha256,
    )
    return _cognition_evidence_from_canonical_comparison(canonical)


def cognition_candidate_from_comparisons(
    *,
    candidate_id: str,
    kind: CognitionCandidateKind,
    statement: str,
    comparisons: tuple[ExperienceMemoryComparison, ...],
    expected_comparator_sha256: str,
    expected_comparison_policy_sha256: str,
) -> CognitionCandidate:
    canonical = _canonical_comparisons(
        comparisons,
        expected_comparator_sha256=expected_comparator_sha256,
        expected_comparison_policy_sha256=expected_comparison_policy_sha256,
    )
    evidence = tuple(
        _cognition_evidence_from_canonical_comparison(item) for item in canonical
    )
    first = canonical[0]
    return CognitionCandidate.create(
        candidate_id=candidate_id,
        workspace_id=first.workspace_id,
        agent_id=first.agent_id,
        kind=kind,
        statement=statement,
        evidence=evidence,
    )
