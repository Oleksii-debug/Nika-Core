"""Regression: verification clearance must be bound to unchanged classified output."""

from __future__ import annotations

import nika_core.product_factory_verification as verification

SHA_A = "a" * 40
SHA_B = "b" * 40


def _classified_pass() -> verification.CandidateVerification:
    evidence = tuple(
        verification.ExactShaCheckEvidence(
            check_id=check_id,
            candidate_sha=SHA_A,
            state=verification.CheckState.PASS,
            evidence_ref=f"actions://{check_id}/success",
        )
        for check_id in verification.PRODUCT_FACTORY_REQUIRED_CHECK_IDS
    )
    return verification.classify_candidate_verification(
        SHA_A, evidence, verification.PRODUCT_FACTORY_REQUIRED_CHECK_IDS
    )


def test_unknown_result_state_mutation_cannot_mint_merge_clearance() -> None:
    result = verification.CandidateVerification(SHA_A, verification.VerificationState.UNKNOWN, ())
    object.__setattr__(result, "state", verification.VerificationState.PASS)

    assert result.merge_clearance is False


def test_verified_result_candidate_sha_mutation_revokes_merge_clearance() -> None:
    result = _classified_pass()
    assert result.merge_clearance is True
    object.__setattr__(result, "candidate_sha", SHA_B)

    assert result.merge_clearance is False


def test_verified_result_evidence_mutation_revokes_merge_clearance() -> None:
    result = _classified_pass()
    assert result.merge_clearance is True
    object.__setattr__(result, "evidence_refs", ())

    assert result.merge_clearance is False


def test_uninitialized_carrier_with_pass_fields_does_not_grant_clearance() -> None:
    result = object.__new__(verification.CandidateVerification)
    object.__setattr__(result, "candidate_sha", SHA_A)
    object.__setattr__(result, "state", verification.VerificationState.PASS)
    object.__setattr__(result, "evidence_refs", ("actions://core/success",))

    assert result.merge_clearance is False


def test_valid_classifier_pass_and_direct_unknown_keep_existing_semantics() -> None:
    assert _classified_pass().merge_clearance is True
    unknown = verification.CandidateVerification(SHA_A, verification.VerificationState.UNKNOWN, ())
    assert unknown.merge_clearance is False
