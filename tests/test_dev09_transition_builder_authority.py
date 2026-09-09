from __future__ import annotations

import pytest

from nika_core.qa.review_state import (
    CandidateReviewIdentity,
    CandidateReviewRecord,
    ReviewerAuthorityEvidence,
    ReviewPipelineError,
    ReviewState,
    ReviewVerdict,
)

SHA_A = "a" * 40


def _merge_ready_inputs() -> tuple[
    CandidateReviewIdentity,
    ReviewerAuthorityEvidence,
    ReviewVerdict,
]:
    identity = CandidateReviewIdentity(
        work_id="work-1",
        candidate_sha=SHA_A,
        implementer_id="dev-1",
    )
    authority = ReviewerAuthorityEvidence(
        candidate_sha=SHA_A,
        reviewer_id="reviewer-1",
        authority_ref="authority://reviewer-1",
        independent_review_authorized=True,
    )
    verdict = ReviewVerdict(
        candidate_sha=SHA_A,
        reviewer_id="reviewer-1",
        accepted=True,
        reason="independent review passed",
        evidence_refs=("qa://pass",),
    )
    return identity, authority, verdict


def test_public_transition_builder_cannot_forge_merge_ready() -> None:
    identity, authority, verdict = _merge_ready_inputs()

    with pytest.raises(ReviewPipelineError):
        CandidateReviewRecord._from_transition(
            identity,
            ReviewState.MERGE_READY,
            reviewer_id="reviewer-1",
            reviewer_authority=authority,
            verdict=verdict,
        )


def test_mangled_transition_builder_requires_exact_head_clearance_for_merge_ready() -> None:
    identity, authority, verdict = _merge_ready_inputs()
    builder = getattr(CandidateReviewRecord, "_CandidateReviewRecord__from_transition")

    with pytest.raises(
        ReviewPipelineError,
        match="MERGE_READY construction requires exact-head verification clearance",
    ):
        builder(
            identity,
            ReviewState.MERGE_READY,
            reviewer_id="reviewer-1",
            reviewer_authority=authority,
            verdict=verdict,
        )
