from __future__ import annotations

import pytest

from nika_core.qa.review_state import CandidateReviewIdentity, CandidateReviewRecord, ReviewState

SHA_A = "a" * 40


def test_public_constructor_cannot_forge_merge_ready_state() -> None:
    identity = CandidateReviewIdentity(
        work_id="work-1",
        candidate_sha=SHA_A,
        implementer_id="dev-1",
    )

    with pytest.raises(TypeError):
        CandidateReviewRecord(
            identity=identity,
            state=ReviewState.MERGE_READY,
        )
