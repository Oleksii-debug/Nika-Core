"""A str-backed enum allocation is not proof of a canonical enum member."""

from __future__ import annotations

import pytest

from nika_core import product_factory_verification as verification

SHA = 'a' * 40
REQUIRED = verification.PRODUCT_FACTORY_REQUIRED_CHECK_IDS


def evidence(state):
    return verification.ExactShaCheckEvidence('core', SHA, state, 'ci:core')


def factory_pass():
    return verification.ExactShaCheckEvidence(
        'factory', SHA, verification.CheckState.PASS, 'ci:factory',
    )


@pytest.mark.parametrize('raw', ['unknown', 'running', 'fail', 'pass', 'not-a-check-state'])
def test_allocated_check_enum_without_member_identity_is_rejected(raw):
    forged = str.__new__(verification.CheckState, raw)
    assert type(forged) is verification.CheckState
    assert all(forged is not member for member in verification.CheckState)
    with pytest.raises(verification.VerificationError, match='check state'):
        evidence(forged)


@pytest.mark.parametrize('raw', ['unknown', 'running', 'fail', 'pass', 'not-a-check-state'])
def test_changed_evidence_state_cannot_fall_through_to_clearance(raw):
    original = evidence(verification.CheckState.UNKNOWN)
    object.__setattr__(original, 'state', str.__new__(verification.CheckState, raw))
    with pytest.raises(verification.VerificationError, match='check state'):
        verification.classify_candidate_verification(SHA, (original, factory_pass()), REQUIRED)


@pytest.mark.parametrize(
    'raw', ['unknown', 'running', 'pass', 'fail', 'stale', 'mismatch', 'not-a-result-state'],
)
def test_allocated_result_enum_without_member_identity_is_rejected(raw):
    forged = str.__new__(verification.VerificationState, raw)
    assert type(forged) is verification.VerificationState
    assert all(forged is not member for member in verification.VerificationState)
    with pytest.raises(verification.VerificationError, match='verification state'):
        verification.CandidateVerification(SHA, forged, ())


def test_string_equal_forged_pass_cannot_seed_classifier_result_identity():
    forged = str.__new__(verification.VerificationState, 'pass')
    try:
        result = verification.CandidateVerification(SHA, forged, ())
    except verification.VerificationError:
        return
    object.__setattr__(result, 'state', verification.VerificationState.PASS)
    assert result.merge_clearance is False


@pytest.mark.parametrize('raw', ['unknown', 'running', 'fail', 'pass'])
def test_explicit_string_to_canonical_member_keeps_existing_classification(raw):
    canonical = verification.CheckState(raw)
    result = verification.classify_candidate_verification(
        SHA, (evidence(canonical), factory_pass()), REQUIRED,
    )
    assert result.state is verification.VerificationState(raw)
    assert result.merge_clearance is (canonical is verification.CheckState.PASS)
