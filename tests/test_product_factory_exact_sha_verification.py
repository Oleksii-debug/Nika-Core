from __future__ import annotations

import pytest

from nika_core import product_factory_verification as verification


SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


def evidence(
    check_id: str,
    sha: str,
    state: verification.CheckState,
    *,
    required: bool = True,
) -> verification.ExactShaCheckEvidence:
    return verification.ExactShaCheckEvidence(
        check_id=check_id,
        candidate_sha=sha,
        state=state,
        evidence_ref=f"actions://{check_id}/{sha}",
        required=required,
    )


def test_no_evidence_is_unknown_and_never_merge_clearance() -> None:
    result = verification.classify_candidate_verification(SHA_A, ())

    assert result == verification.CandidateVerification(
        SHA_A, verification.VerificationState.UNKNOWN, ()
    )
    assert result.merge_clearance is False


def test_all_required_exact_head_checks_must_pass_for_clearance() -> None:
    result = verification.classify_candidate_verification(
        SHA_A,
        (
            evidence("core", SHA_A, verification.CheckState.PASS),
            evidence("factory", SHA_A, verification.CheckState.PASS),
            evidence("optional", SHA_A, verification.CheckState.FAIL, required=False),
        ),
    )

    assert result.state is verification.VerificationState.PASS
    assert result.merge_clearance is True


@pytest.mark.parametrize(
    ("check_state", "expected"),
    (
        (verification.CheckState.RUNNING, verification.VerificationState.RUNNING),
        (verification.CheckState.UNKNOWN, verification.VerificationState.UNKNOWN),
        (verification.CheckState.FAIL, verification.VerificationState.FAIL),
    ),
)
def test_nonpassing_required_check_cannot_become_merge_clearance(
    check_state: verification.CheckState,
    expected: verification.VerificationState,
) -> None:
    result = verification.classify_candidate_verification(
        SHA_A,
        (
            evidence("core", SHA_A, verification.CheckState.PASS),
            evidence("required", SHA_A, check_state),
        ),
    )

    assert result.state is expected
    assert result.merge_clearance is False


def test_prior_head_green_is_stale_after_candidate_changes() -> None:
    old_green = (
        evidence("core", SHA_A, verification.CheckState.PASS),
        evidence("factory", SHA_A, verification.CheckState.PASS),
    )

    result = verification.classify_candidate_verification(SHA_B, old_green)

    assert result.state is verification.VerificationState.STALE
    assert result.merge_clearance is False


def test_mixed_sha_evidence_is_mismatch_even_when_current_head_passes() -> None:
    result = verification.classify_candidate_verification(
        SHA_B,
        (
            evidence("core-current", SHA_B, verification.CheckState.PASS),
            evidence("factory-old", SHA_A, verification.CheckState.PASS),
        ),
    )

    assert result.state is verification.VerificationState.MISMATCH
    assert result.merge_clearance is False


def test_multiple_foreign_heads_are_mismatch_not_stale() -> None:
    result = verification.classify_candidate_verification(
        SHA_C,
        (
            evidence("core-a", SHA_A, verification.CheckState.PASS),
            evidence("core-b", SHA_B, verification.CheckState.PASS),
        ),
    )

    assert result.state is verification.VerificationState.MISMATCH


def test_duplicate_evidence_refs_are_rejected() -> None:
    first = evidence("core", SHA_A, verification.CheckState.PASS)
    duplicate = verification.ExactShaCheckEvidence(
        check_id="factory",
        candidate_sha=SHA_A,
        state=verification.CheckState.PASS,
        evidence_ref=first.evidence_ref,
    )

    with pytest.raises(verification.VerificationError, match="refs must be unique"):
        verification.classify_candidate_verification(SHA_A, (first, duplicate))


def test_invalid_candidate_identity_is_rejected() -> None:
    with pytest.raises(verification.VerificationError, match="candidate SHA"):
        verification.classify_candidate_verification("not-a-sha", ())
