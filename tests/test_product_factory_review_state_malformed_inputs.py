import json
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


@pytest.mark.parametrize(
    ("section", "field", "value"),
    (
        ("identity", "implementer_id", None),
        ("identity", "work_id", 42),
        ("reviewer_authority", "authority_ref", None),
        ("verdict", "reason", None),
        ("verdict", "evidence_refs", [None]),
    ),
)
def test_restore_normalizes_non_string_persisted_fields(
    section: str,
    field: str,
    value: object,
) -> None:
    raw = json.loads(_passed_review().snapshot())
    raw[section][field] = value

    with pytest.raises(ReviewPipelineError, match="review snapshot is invalid"):
        CandidateReviewRecord.restore(json.dumps(raw))


def test_restore_normalizes_non_string_assigned_reviewer() -> None:
    raw = json.loads(_passed_review().snapshot())
    raw["reviewer_id"] = 42

    with pytest.raises(ReviewPipelineError, match="review snapshot is invalid"):
        CandidateReviewRecord.restore(json.dumps(raw))
