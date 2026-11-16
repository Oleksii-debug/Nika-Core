from __future__ import annotations

import pytest

import nika_core.product_factory_verification as verification

SHA = "a" * 40


def _pass(check_id: str) -> verification.ExactShaCheckEvidence:
    return verification.ExactShaCheckEvidence(
        check_id=check_id,
        candidate_sha=SHA,
        state=verification.CheckState.PASS,
        evidence_ref=f"actions://{check_id}/{SHA}",
    )


def test_caller_cannot_omit_authoritative_required_gate_and_mint_clearance() -> None:
    with pytest.raises(verification.VerificationError, match="authoritative Product Factory profile"):
        verification.classify_candidate_verification(
            SHA,
            (_pass("core"),),
            ("core",),
        )


def test_caller_cannot_substitute_authoritative_required_gate_and_mint_clearance() -> None:
    with pytest.raises(verification.VerificationError, match="authoritative Product Factory profile"):
        verification.classify_candidate_verification(
            SHA,
            (_pass("core"), _pass("replacement")),
            ("core", "replacement"),
        )


def test_canonical_profile_still_requires_every_exact_head_gate_to_pass() -> None:
    result = verification.classify_candidate_verification(
        SHA,
        (_pass("core"), _pass("factory")),
        verification.PRODUCT_FACTORY_REQUIRED_CHECK_IDS,
    )

    assert result.state is verification.VerificationState.PASS
    assert result.merge_clearance is True
