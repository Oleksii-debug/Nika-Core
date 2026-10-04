"""Untrusted verification identities must not spoof audit or status text."""

from __future__ import annotations

import pytest

import nika_core.product_factory_verification as verification

SHA = "a" * 40
REQUIRED = verification.PRODUCT_FACTORY_REQUIRED_CHECK_IDS


def _evidence(
    *, check_id: str = "core", evidence_ref: str = "actions://core/pass"
) -> verification.ExactShaCheckEvidence:
    return verification.ExactShaCheckEvidence(
        check_id=check_id,
        candidate_sha=SHA,
        state=verification.CheckState.PASS,
        evidence_ref=evidence_ref,
    )


@pytest.mark.parametrize("hidden", ("\n", "\r", "\t", "\x00", "\x7f", "\u202e", "\u2066", "\u200b"))
def test_check_id_rejects_embedded_controls_and_invisible_text(hidden: str) -> None:
    with pytest.raises(verification.VerificationError, match="check id must be printable"):
        _evidence(check_id=f"optional{hidden}core")


@pytest.mark.parametrize("hidden", ("\n", "\r", "\t", "\x00", "\x7f", "\u202e", "\u2066", "\u200b"))
def test_evidence_ref_rejects_embedded_controls_and_invisible_text(hidden: str) -> None:
    with pytest.raises(verification.VerificationError, match="evidence ref must be printable"):
        _evidence(evidence_ref=f"actions://core/{hidden}success")


def test_mutated_evidence_is_rechecked_before_classification() -> None:
    core = _evidence()
    object.__setattr__(core, "evidence_ref", "actions://core/pass\nFAKE PASS")
    factory = _evidence(check_id="factory", evidence_ref="actions://factory/pass")
    with pytest.raises(verification.VerificationError, match="evidence ref must be printable"):
        verification.classify_candidate_verification(SHA, (core, factory), REQUIRED)


def test_direct_verification_carrier_rejects_unprintable_reference() -> None:
    with pytest.raises(verification.VerificationError, match="evidence ref must be printable"):
        verification.CandidateVerification(
            SHA, verification.VerificationState.UNKNOWN, ("actions://core/pass\rspoof",)
        )


def test_required_profile_rejects_invisible_text() -> None:
    with pytest.raises(verification.VerificationError, match="required check id must be printable"):
        verification.classify_candidate_verification(SHA, (), ("core", "factory\u202e"))


def test_printable_unicode_references_keep_canonical_pass() -> None:
    core = _evidence(evidence_ref="actions://перевірка/успішно")
    factory = _evidence(check_id="factory", evidence_ref="actions://збірка/успішно")
    result = verification.classify_candidate_verification(SHA, (core, factory), REQUIRED)
    assert result.state is verification.VerificationState.PASS
    assert result.merge_clearance is True
