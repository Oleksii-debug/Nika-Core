"""Regression: bounded verification evidence must fail before expensive processing."""

from __future__ import annotations

import pytest

import nika_core.product_factory_verification as verification

SHA = "a" * 40
REQUIRED = verification.PRODUCT_FACTORY_REQUIRED_CHECK_IDS


def _pass(check_id: str, *, required: bool = True) -> verification.ExactShaCheckEvidence:
    return verification.ExactShaCheckEvidence(
        check_id=check_id,
        candidate_sha=SHA,
        state=verification.CheckState.PASS,
        evidence_ref=f"actions://{check_id}/pass",
        required=required,
    )


def test_check_id_accepts_exact_maximum_length() -> None:
    assert len(_pass("x" * verification.MAX_CHECK_ID_LENGTH).check_id) == (
        verification.MAX_CHECK_ID_LENGTH
    )


def test_oversized_check_id_fails_before_utf8_encoding() -> None:
    with pytest.raises(verification.VerificationError, match="check id exceeds maximum length"):
        _pass("\ud800" * (verification.MAX_CHECK_ID_LENGTH + 1))


def test_mutated_oversized_check_id_is_revalidated_during_snapshot() -> None:
    forged = _pass("core")
    object.__setattr__(forged, "check_id", "x" * (verification.MAX_CHECK_ID_LENGTH + 1))

    with pytest.raises(verification.VerificationError, match="check id exceeds maximum length"):
        verification.classify_candidate_verification(SHA, (forged, _pass("factory")), REQUIRED)


def test_oversized_reference_fails_before_utf8_encoding() -> None:
    with pytest.raises(verification.VerificationError, match="ref exceeds maximum length"):
        verification.ExactShaCheckEvidence(
            check_id="core",
            candidate_sha=SHA,
            state=verification.CheckState.PASS,
            evidence_ref="\ud800" * (verification.MAX_EVIDENCE_REF_LENGTH + 1),
        )


def test_oversized_evidence_rejected_before_snapshot_or_dedup() -> None:
    duplicate = _pass("core")
    evidence = (duplicate,) * (verification.MAX_EVIDENCE_ITEMS + 1)

    with pytest.raises(verification.VerificationError, match="exceeds maximum item count"):
        verification.classify_candidate_verification(SHA, evidence, REQUIRED)


def test_exact_evidence_count_boundary_preserves_canonical_clearance() -> None:
    evidence = (_pass("core"), _pass("factory")) + tuple(
        _pass(f"optional-{i}", required=False)
        for i in range(verification.MAX_EVIDENCE_ITEMS - 2)
    )

    result = verification.classify_candidate_verification(SHA, evidence, REQUIRED)

    assert len(result.evidence_refs) == verification.MAX_EVIDENCE_ITEMS
    assert result.merge_clearance is True


def test_direct_candidate_cannot_bypass_evidence_count_bound() -> None:
    refs = tuple(
        f"actions://optional-{i}/pass"
        for i in range(verification.MAX_EVIDENCE_ITEMS + 1)
    )

    with pytest.raises(verification.VerificationError, match="refs exceed maximum item count"):
        verification.CandidateVerification(SHA, verification.VerificationState.UNKNOWN, refs)


def test_oversized_required_profile_rejected_before_element_processing() -> None:
    class HostileText(str):
        def strip(self, chars=None):
            raise AssertionError("oversized profile must not inspect element behavior")

    with pytest.raises(verification.VerificationError, match="authoritative profile size"):
        verification.classify_candidate_verification(
            SHA, (), ("core", "factory", HostileText("surprise"))
        )


def test_oversized_required_check_id_rejected_before_utf8_encoding() -> None:
    with pytest.raises(verification.VerificationError, match="required check id exceeds"):
        verification.classify_candidate_verification(
            SHA, (), ("core", "\ud800" * (verification.MAX_CHECK_ID_LENGTH + 1))
        )
