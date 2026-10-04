"""Reject malformed verification batches before deriving any SHA outcome."""

from __future__ import annotations

import pytest

import nika_core.product_factory_verification as verification

CURRENT_SHA = "a" * 40
FOREIGN_SHA = "b" * 40
REQUIRED = verification.PRODUCT_FACTORY_REQUIRED_CHECK_IDS


def _check(
    check_id: str,
    sha: str,
    *,
    suffix: str = "1",
    required: bool = True,
) -> verification.ExactShaCheckEvidence:
    return verification.ExactShaCheckEvidence(
        check_id=check_id,
        candidate_sha=sha,
        state=verification.CheckState.PASS,
        evidence_ref=f"actions://{check_id}/{sha}/{suffix}",
        required=required,
    )


@pytest.mark.parametrize(
    "checks",
    (
        (_check("core", FOREIGN_SHA), _check("core", FOREIGN_SHA, suffix="2")),
        (_check("core", CURRENT_SHA), _check("core", FOREIGN_SHA)),
    ),
)
def test_duplicate_check_id_is_invalid_even_for_stale_or_mixed_sha(
    checks: tuple[verification.ExactShaCheckEvidence, ...],
) -> None:
    with pytest.raises(verification.VerificationError, match="check ids must be unique"):
        verification.classify_candidate_verification(CURRENT_SHA, checks, REQUIRED)


@pytest.mark.parametrize(
    "checks",
    (
        (_check("rogue", FOREIGN_SHA),),
        (_check("core", CURRENT_SHA), _check("rogue", FOREIGN_SHA)),
        (_check("core", CURRENT_SHA), _check("rogue", CURRENT_SHA)),
    ),
)
def test_unapproved_required_gate_is_invalid_before_sha_or_missing_gate_classification(
    checks: tuple[verification.ExactShaCheckEvidence, ...],
) -> None:
    with pytest.raises(verification.VerificationError, match="not authoritative"):
        verification.classify_candidate_verification(CURRENT_SHA, checks, REQUIRED)


def test_legitimate_foreign_sha_remains_stale_without_structural_defect() -> None:
    result = verification.classify_candidate_verification(
        CURRENT_SHA, (_check("core", FOREIGN_SHA), _check("factory", FOREIGN_SHA)), REQUIRED
    )
    assert result.state is verification.VerificationState.STALE
    assert result.merge_clearance is False


def test_legitimate_mixed_sha_remains_mismatch_without_structural_defect() -> None:
    result = verification.classify_candidate_verification(
        CURRENT_SHA, (_check("core", CURRENT_SHA), _check("factory", FOREIGN_SHA)), REQUIRED
    )
    assert result.state is verification.VerificationState.MISMATCH
    assert result.merge_clearance is False


def test_canonical_pass_and_incomplete_profile_keep_existing_semantics() -> None:
    core = _check("core", CURRENT_SHA)
    factory = _check("factory", CURRENT_SHA)
    passed = verification.classify_candidate_verification(CURRENT_SHA, (core, factory), REQUIRED)
    assert passed.state is verification.VerificationState.PASS
    assert passed.merge_clearance is True

    missing = verification.classify_candidate_verification(CURRENT_SHA, (core,), REQUIRED)
    assert missing.state is verification.VerificationState.UNKNOWN
    assert missing.merge_clearance is False
