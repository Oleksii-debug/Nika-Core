from __future__ import annotations

import pytest

from nika_core.learning_verification import (
    CandidateDatasetVerification,
    LearningVerificationValidationError,
    VerificationCheckEvidence,
    VerificationOutcome,
)


_SHA_A = "a" * 64
_SHA_B = "b" * 64
_SHA_C = "c" * 64


class _PayloadSpoofingEvidence(VerificationCheckEvidence):
    def canonical_payload(self) -> dict[str, object]:
        payload = super().canonical_payload()
        payload["outcome"] = VerificationOutcome.FAIL.value
        return payload


def test_candidate_verification_rejects_evidence_subclasses_before_trust() -> None:
    spoofed = _PayloadSpoofingEvidence(
        check_id="safety",
        checker_sha256=_SHA_A,
        evidence_sha256=_SHA_B,
        outcome=VerificationOutcome.PASS,
    )

    with pytest.raises(
        LearningVerificationValidationError,
        match="exact VerificationCheckEvidence",
    ):
        CandidateDatasetVerification.create(
            candidate_material_sha256=_SHA_C,
            verification_policy_sha256=_SHA_A,
            required_check_ids=("safety",),
            checks=(spoofed,),
        )
