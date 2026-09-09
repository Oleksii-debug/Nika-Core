from dataclasses import dataclass

import pytest

from nika_core.qa.review_state import (
    CandidateReviewIdentity,
    CandidateReviewRecord,
    ReviewPipelineError,
)

SHA_A = "a" * 40


@dataclass(frozen=True)
class _Authority:
    candidate_sha: str = SHA_A
    reviewer_id: str = "qa-1"
    authority_ref: str = "authority:qa-assignment-1"
    independent_review_authorized: bool = True


def _passed_review() -> CandidateReviewRecord:
    candidate = CandidateReviewRecord(
        CandidateReviewIdentity("work-1", SHA_A, "dev-1")
    )
    return (
        candidate.require_review()
        .queue_qa(authority=_Authority())
        .start_qa(reviewer_id="qa-1")
        .record_verdict(
            candidate_sha=SHA_A,
            reviewer_id="qa-1",
            accepted=True,
            reason="pass",
            evidence_refs=("ci:run-1",),
        )
    )


def test_malformed_reviewer_authority_fails_closed() -> None:
    candidate = CandidateReviewRecord(
        CandidateReviewIdentity("work-1", SHA_A, "dev-1")
    ).require_review()

    with pytest.raises(ReviewPipelineError, match="reviewer authority is malformed"):
        candidate.queue_qa(authority=object())  # type: ignore[arg-type]


def test_malformed_exact_head_clearance_fails_closed() -> None:
    with pytest.raises(
        ReviewPipelineError,
        match="exact-head verification clearance is malformed",
    ):
        _passed_review().mark_merge_ready(
            candidate_sha=SHA_A,
            verification=object(),  # type: ignore[arg-type]
        )
