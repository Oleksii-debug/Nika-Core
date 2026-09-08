from __future__ import annotations

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


def _pending_review() -> CandidateReviewRecord:
    candidate = CandidateReviewRecord(
        CandidateReviewIdentity(
            work_id="work-1",
            candidate_sha=SHA_A,
            implementer_id="dev-1",
        )
    )
    return candidate.require_review().queue_qa(authority=_Authority())


def test_snapshot_persists_explicit_independent_review_authorization() -> None:
    payload = _pending_review().snapshot()
    authority = json.loads(payload)["reviewer_authority"]

    assert authority["candidate_sha"] == SHA_A
    assert authority["reviewer_id"] == "qa-1"
    assert authority["authority_ref"] == "authority:qa-assignment-1"
    assert authority["independent_review_authorized"] is True


def test_restore_rejects_tampered_independent_review_authorization() -> None:
    payload = _pending_review().snapshot().replace(
        '"independent_review_authorized":true',
        '"independent_review_authorized":false',
    )

    with pytest.raises(ReviewPipelineError, match="explicit independent authorization"):
        CandidateReviewRecord.restore(payload)


def test_restore_accepts_legacy_authorized_snapshot_without_decision_field() -> None:
    payload = _pending_review().snapshot().replace(
        ',"independent_review_authorized":true',
        "",
    )

    restored = CandidateReviewRecord.restore(payload)

    assert restored.reviewer_authority is not None
    assert restored.reviewer_authority.independent_review_authorized is True
