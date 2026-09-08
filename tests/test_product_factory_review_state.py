from __future__ import annotations

import pytest

from nika_core.qa.review_state import (
    CandidateReviewIdentity,
    CandidateReviewRecord,
    ReviewPipelineError,
    ReviewState,
    StaleCandidateReviewError,
)

SHA_A = "a" * 40
SHA_B = "b" * 40


def _candidate() -> CandidateReviewRecord:
    return CandidateReviewRecord(
        CandidateReviewIdentity(
            work_id="work-1",
            candidate_sha=SHA_A,
            implementer_id="dev-1",
        )
    )


def _running_review() -> CandidateReviewRecord:
    return (
        _candidate()
        .require_review()
        .queue_qa(reviewer_id="qa-1")
        .start_qa(reviewer_id="qa-1")
    )


def test_pass_journey_is_exact_sha_and_restart_safe() -> None:
    pending = _candidate().require_review().queue_qa(reviewer_id="qa-1")
    pending = CandidateReviewRecord.restore(pending.snapshot())
    assert pending.state is ReviewState.QA_PENDING

    passed = pending.start_qa(reviewer_id="qa-1").record_verdict(
        candidate_sha=SHA_A,
        reviewer_id="qa-1",
        accepted=True,
        reason="independent exact-head review passed",
        evidence_refs=("ci:run-123", "review:evidence-456"),
    )
    restored = CandidateReviewRecord.restore(passed.snapshot())

    assert restored.state is ReviewState.PASS
    assert restored.mark_merge_ready(candidate_sha=SHA_A).state is ReviewState.MERGE_READY


def test_failed_review_must_transition_to_fix_required() -> None:
    failed = _running_review().record_verdict(
        candidate_sha=SHA_A,
        reviewer_id="qa-1",
        accepted=False,
        reason="bounded defect evidence",
        evidence_refs=("defect:1",),
    )

    assert failed.state is ReviewState.FAIL
    assert failed.require_fix(candidate_sha=SHA_A).state is ReviewState.FIX_REQUIRED


def test_candidate_implementer_cannot_self_review() -> None:
    with pytest.raises(ReviewPipelineError, match="cannot independently review own work"):
        _candidate().require_review().queue_qa(reviewer_id="dev-1")


@pytest.mark.parametrize("implementer_id", [" dev-1", "dev-1 ", "\tdev-1"])
def test_candidate_implementer_identity_must_be_canonical(implementer_id: str) -> None:
    with pytest.raises(ReviewPipelineError, match="implementer identity must be canonical"):
        CandidateReviewIdentity(
            work_id="work-1",
            candidate_sha=SHA_A,
            implementer_id=implementer_id,
        )


@pytest.mark.parametrize("reviewer_id", [" dev-1", "dev-1 ", "\tdev-1"])
def test_edge_whitespace_cannot_bypass_self_review_identity(reviewer_id: str) -> None:
    with pytest.raises(ReviewPipelineError, match="reviewer identity must be canonical"):
        _candidate().require_review().queue_qa(reviewer_id=reviewer_id)


def test_work_identity_must_be_canonical() -> None:
    with pytest.raises(ReviewPipelineError, match="candidate work identity must be canonical"):
        CandidateReviewIdentity(
            work_id="work-1 ",
            candidate_sha=SHA_A,
            implementer_id="dev-1",
        )


def test_stale_candidate_sha_cannot_receive_verdict() -> None:
    with pytest.raises(StaleCandidateReviewError, match="exact current candidate SHA"):
        _running_review().record_verdict(
            candidate_sha=SHA_B,
            reviewer_id="qa-1",
            accepted=True,
            reason="wrong head",
            evidence_refs=("ci:wrong-head",),
        )


def test_successor_head_invalidates_prior_clearance() -> None:
    merge_ready = _running_review().record_verdict(
        candidate_sha=SHA_A,
        reviewer_id="qa-1",
        accepted=True,
        reason="pass",
        evidence_refs=("ci:1",),
    ).mark_merge_ready(candidate_sha=SHA_A)

    successor = merge_ready.successor(candidate_sha=SHA_B, implementer_id="dev-2")

    assert successor.identity.candidate_sha == SHA_B
    assert successor.state is ReviewState.IMPLEMENTED
    assert successor.reviewer_id is None
    assert successor.verdict is None


def test_review_evidence_is_bounded() -> None:
    with pytest.raises(ReviewPipelineError, match="bounded evidence references"):
        _running_review().record_verdict(
            candidate_sha=SHA_A,
            reviewer_id="qa-1",
            accepted=True,
            reason="pass",
            evidence_refs=tuple(f"evidence:{index}" for index in range(17)),
        )


def test_restored_tampered_verdict_is_rejected() -> None:
    passed = _running_review().record_verdict(
        candidate_sha=SHA_A,
        reviewer_id="qa-1",
        accepted=True,
        reason="pass",
        evidence_refs=("ci:1",),
    )
    payload = passed.snapshot().replace(SHA_A, SHA_B, 1)

    with pytest.raises(ReviewPipelineError):
        CandidateReviewRecord.restore(payload)
