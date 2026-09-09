from __future__ import annotations

import pytest

import nika_core.product_factory_verification as verification

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
REQUIRED = ("core", "factory")


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
    result = verification.classify_candidate_verification(SHA_A, (), REQUIRED)

    assert result == verification.CandidateVerification(
        SHA_A, verification.VerificationState.UNKNOWN, ()
    )
    assert result.merge_clearance is False


def test_missing_required_check_is_unknown_and_never_merge_clearance() -> None:
    result = verification.classify_candidate_verification(
        SHA_A,
        (evidence("core", SHA_A, verification.CheckState.PASS),),
        REQUIRED,
    )

    assert result.state is verification.VerificationState.UNKNOWN
    assert result.merge_clearance is False


def test_all_required_exact_head_checks_must_pass_for_clearance() -> None:
    result = verification.classify_candidate_verification(
        SHA_A,
        (
            evidence("core", SHA_A, verification.CheckState.PASS),
            evidence("factory", SHA_A, verification.CheckState.PASS),
            evidence("optional", SHA_A, verification.CheckState.FAIL, required=False),
        ),
        REQUIRED,
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
            evidence("factory", SHA_A, check_state),
        ),
        REQUIRED,
    )

    assert result.state is expected
    assert result.merge_clearance is False


def test_unknown_self_declared_required_gate_is_rejected() -> None:
    with pytest.raises(verification.VerificationError, match="not authoritative"):
        verification.classify_candidate_verification(
            SHA_A,
            (
                evidence("core", SHA_A, verification.CheckState.PASS),
                evidence("factory", SHA_A, verification.CheckState.PASS),
                evidence("surprise", SHA_A, verification.CheckState.PASS),
            ),
            REQUIRED,
        )


def test_authoritative_required_set_overrides_optional_evidence_flag() -> None:
    result = verification.classify_candidate_verification(
        SHA_A,
        (
            evidence("core", SHA_A, verification.CheckState.PASS),
            evidence("factory", SHA_A, verification.CheckState.FAIL, required=False),
        ),
        REQUIRED,
    )

    assert result.state is verification.VerificationState.FAIL
    assert result.merge_clearance is False


def test_prior_head_green_is_stale_after_candidate_changes() -> None:
    old_green = (
        evidence("core", SHA_A, verification.CheckState.PASS),
        evidence("factory", SHA_A, verification.CheckState.PASS),
    )

    result = verification.classify_candidate_verification(SHA_B, old_green, REQUIRED)

    assert result.state is verification.VerificationState.STALE
    assert result.merge_clearance is False


def test_mixed_sha_evidence_is_mismatch_even_when_current_head_passes() -> None:
    result = verification.classify_candidate_verification(
        SHA_B,
        (
            evidence("core", SHA_B, verification.CheckState.PASS),
            evidence("factory", SHA_A, verification.CheckState.PASS),
        ),
        REQUIRED,
    )

    assert result.state is verification.VerificationState.MISMATCH
    assert result.merge_clearance is False


def test_multiple_foreign_heads_are_mismatch_not_stale() -> None:
    result = verification.classify_candidate_verification(
        SHA_C,
        (
            evidence("core", SHA_A, verification.CheckState.PASS),
            evidence("factory", SHA_B, verification.CheckState.PASS),
        ),
        REQUIRED,
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
        verification.classify_candidate_verification(SHA_A, (first, duplicate), REQUIRED)


def test_duplicate_check_ids_are_rejected() -> None:
    with pytest.raises(verification.VerificationError, match="check ids must be unique"):
        verification.classify_candidate_verification(
            SHA_A,
            (
                evidence("core", SHA_A, verification.CheckState.PASS),
                verification.ExactShaCheckEvidence(
                    check_id="core",
                    candidate_sha=SHA_A,
                    state=verification.CheckState.PASS,
                    evidence_ref="actions://core/duplicate",
                ),
            ),
            ("core",),
        )


@pytest.mark.parametrize("required_check_ids", ((), ("core", "core"), ("core", "")))
def test_required_check_identity_set_is_validated(
    required_check_ids: tuple[str, ...],
) -> None:
    with pytest.raises(verification.VerificationError, match="required check ids"):
        verification.classify_candidate_verification(SHA_A, (), required_check_ids)


def test_candidate_verification_state_is_closed() -> None:
    with pytest.raises(verification.VerificationError, match="verification state"):
        verification.CandidateVerification(
            SHA_A,
            "pass",  # type: ignore[arg-type]
            (),
        )


def test_required_check_identity_type_is_bounded() -> None:
    with pytest.raises(verification.VerificationError, match="required check ids"):
        verification.classify_candidate_verification(
            SHA_A,
            (),
            ("core", 1),  # type: ignore[arg-type]
        )


def test_required_flag_must_be_a_real_bool() -> None:
    with pytest.raises(verification.VerificationError, match="required flag"):
        verification.ExactShaCheckEvidence(
            check_id="core",
            candidate_sha=SHA_A,
            state=verification.CheckState.PASS,
            evidence_ref="actions://core/required",
            required=1,  # type: ignore[arg-type]
        )


def test_malformed_evidence_object_is_rejected_fail_closed() -> None:
    with pytest.raises(verification.VerificationError, match="ExactShaCheckEvidence"):
        verification.classify_candidate_verification(
            SHA_A,
            (object(),),  # type: ignore[arg-type]
            REQUIRED,
        )


def test_malformed_evidence_identity_is_rejected_fail_closed() -> None:
    with pytest.raises(verification.VerificationError, match="identity must be text"):
        verification.ExactShaCheckEvidence(
            check_id=1,  # type: ignore[arg-type]
            candidate_sha=SHA_A,
            state=verification.CheckState.PASS,
            evidence_ref="actions://core/malformed",
        )


def test_malformed_required_check_state_is_rejected_fail_closed() -> None:
    with pytest.raises(verification.VerificationError, match="check state"):
        verification.ExactShaCheckEvidence(
            check_id="core",
            candidate_sha=SHA_A,
            state="garbage",  # type: ignore[arg-type]
            evidence_ref="actions://core/malformed",
        )


def test_invalid_candidate_identity_is_rejected() -> None:
    with pytest.raises(verification.VerificationError, match="candidate SHA"):
        verification.classify_candidate_verification("not-a-sha", (), REQUIRED)


def test_evidence_ref_accepts_exact_maximum_boundary() -> None:
    ref = "x" * verification.MAX_EVIDENCE_REF_LENGTH

    observed = verification.ExactShaCheckEvidence(
        check_id="core",
        candidate_sha=SHA_A,
        state=verification.CheckState.PASS,
        evidence_ref=ref,
    )

    assert observed.evidence_ref == ref


def test_evidence_ref_rejects_over_maximum_boundary() -> None:
    ref = "x" * (verification.MAX_EVIDENCE_REF_LENGTH + 1)

    with pytest.raises(verification.VerificationError, match="exceeds maximum length"):
        verification.ExactShaCheckEvidence(
            check_id="core",
            candidate_sha=SHA_A,
            state=verification.CheckState.PASS,
            evidence_ref=ref,
        )


def test_direct_candidate_verification_cannot_bypass_evidence_ref_bound() -> None:
    ref = "x" * (verification.MAX_EVIDENCE_REF_LENGTH + 1)

    with pytest.raises(verification.VerificationError, match="exceeds maximum length"):
        verification.CandidateVerification(
            SHA_A,
            verification.VerificationState.UNKNOWN,
            (ref,),
        )
