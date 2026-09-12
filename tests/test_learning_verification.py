from __future__ import annotations

import hashlib
import json

import pytest

from nika_core.learning_verification import (
    CandidateDatasetVerification,
    LearningVerificationIntegrityError,
    LearningVerificationRejectedError,
    LearningVerificationValidationError,
    VerificationCheckEvidence,
    VerificationOutcome,
)

MATERIAL = "1" * 64
POLICY = "2" * 64
CHECKER_A = "3" * 64
CHECKER_B = "4" * 64
EVIDENCE_A = "5" * 64
EVIDENCE_B = "6" * 64


def _check(
    check_id: str,
    *,
    checker: str,
    evidence: str,
    outcome: VerificationOutcome = VerificationOutcome.PASS,
) -> VerificationCheckEvidence:
    return VerificationCheckEvidence(
        check_id=check_id,
        checker_sha256=checker,
        evidence_sha256=evidence,
        outcome=outcome,
    )


def _verified_receipt() -> CandidateDatasetVerification:
    return CandidateDatasetVerification.create(
        candidate_material_sha256=MATERIAL,
        verification_policy_sha256=POLICY,
        required_check_ids=("quality", "integrity"),
        checks=(
            _check("quality", checker=CHECKER_B, evidence=EVIDENCE_B),
            _check("integrity", checker=CHECKER_A, evidence=EVIDENCE_A),
        ),
    )


def test_create_canonicalizes_required_checks_and_evidence() -> None:
    receipt = _verified_receipt()

    assert receipt.required_check_ids == ("integrity", "quality")
    assert tuple(item.check_id for item in receipt.checks) == ("integrity", "quality")
    assert receipt.is_verified is True
    assert receipt.verification_sha256 == receipt.receipt_sha256


def test_verification_digest_binds_material_policy_checker_and_evidence() -> None:
    baseline = _verified_receipt().verification_sha256

    variants = (
        CandidateDatasetVerification.create(
            candidate_material_sha256="a" * 64,
            verification_policy_sha256=POLICY,
            required_check_ids=("integrity", "quality"),
            checks=(
                _check("integrity", checker=CHECKER_A, evidence=EVIDENCE_A),
                _check("quality", checker=CHECKER_B, evidence=EVIDENCE_B),
            ),
        ),
        CandidateDatasetVerification.create(
            candidate_material_sha256=MATERIAL,
            verification_policy_sha256="b" * 64,
            required_check_ids=("integrity", "quality"),
            checks=(
                _check("integrity", checker=CHECKER_A, evidence=EVIDENCE_A),
                _check("quality", checker=CHECKER_B, evidence=EVIDENCE_B),
            ),
        ),
        CandidateDatasetVerification.create(
            candidate_material_sha256=MATERIAL,
            verification_policy_sha256=POLICY,
            required_check_ids=("integrity", "quality"),
            checks=(
                _check("integrity", checker="c" * 64, evidence=EVIDENCE_A),
                _check("quality", checker=CHECKER_B, evidence=EVIDENCE_B),
            ),
        ),
        CandidateDatasetVerification.create(
            candidate_material_sha256=MATERIAL,
            verification_policy_sha256=POLICY,
            required_check_ids=("integrity", "quality"),
            checks=(
                _check("integrity", checker=CHECKER_A, evidence="d" * 64),
                _check("quality", checker=CHECKER_B, evidence=EVIDENCE_B),
            ),
        ),
    )

    assert all(item.verification_sha256 != baseline for item in variants)


def test_failed_required_check_never_exposes_verification_sha256() -> None:
    receipt = CandidateDatasetVerification.create(
        candidate_material_sha256=MATERIAL,
        verification_policy_sha256=POLICY,
        required_check_ids=("integrity",),
        checks=(
            _check(
                "integrity",
                checker=CHECKER_A,
                evidence=EVIDENCE_A,
                outcome=VerificationOutcome.FAIL,
            ),
        ),
    )

    assert receipt.is_verified is False
    assert len(receipt.receipt_sha256) == 64
    with pytest.raises(LearningVerificationRejectedError):
        _ = receipt.verification_sha256


@pytest.mark.parametrize(
    ("required", "checks"),
    [
        (
            ("integrity", "quality"),
            (_check("integrity", checker=CHECKER_A, evidence=EVIDENCE_A),),
        ),
        (
            ("integrity",),
            (
                _check("integrity", checker=CHECKER_A, evidence=EVIDENCE_A),
                _check("quality", checker=CHECKER_B, evidence=EVIDENCE_B),
            ),
        ),
    ],
)
def test_required_check_set_must_match_evidence_exactly(
    required: tuple[str, ...],
    checks: tuple[VerificationCheckEvidence, ...],
) -> None:
    with pytest.raises(
        LearningVerificationValidationError,
        match="exactly match required_check_ids",
    ):
        CandidateDatasetVerification.create(
            candidate_material_sha256=MATERIAL,
            verification_policy_sha256=POLICY,
            required_check_ids=required,
            checks=checks,
        )


def test_duplicate_required_check_ids_are_rejected() -> None:
    with pytest.raises(
        LearningVerificationValidationError,
        match="required check identifiers must be unique",
    ):
        CandidateDatasetVerification.create(
            candidate_material_sha256=MATERIAL,
            verification_policy_sha256=POLICY,
            required_check_ids=("integrity", "integrity"),
            checks=(
                _check("integrity", checker=CHECKER_A, evidence=EVIDENCE_A),
            ),
        )


def test_duplicate_evidence_check_ids_are_rejected() -> None:
    with pytest.raises(
        LearningVerificationValidationError,
        match="verification check identifiers must be unique",
    ):
        CandidateDatasetVerification.create(
            candidate_material_sha256=MATERIAL,
            verification_policy_sha256=POLICY,
            required_check_ids=("integrity",),
            checks=(
                _check("integrity", checker=CHECKER_A, evidence=EVIDENCE_A),
                _check("integrity", checker=CHECKER_B, evidence=EVIDENCE_B),
            ),
        )


@pytest.mark.parametrize(
    "field_value",
    ["A" * 64, "f" * 63, "g" * 64, "", 123],
)
def test_hash_fields_are_strict_lowercase_sha256(field_value: object) -> None:
    with pytest.raises(LearningVerificationValidationError):
        VerificationCheckEvidence(
            check_id="integrity",
            checker_sha256=field_value,
            evidence_sha256=EVIDENCE_A,
            outcome=VerificationOutcome.PASS,
        )


def test_direct_constructor_rejects_noncanonical_order() -> None:
    with pytest.raises(
        LearningVerificationValidationError,
        match="canonical order",
    ):
        CandidateDatasetVerification(
            candidate_material_sha256=MATERIAL,
            verification_policy_sha256=POLICY,
            required_check_ids=("quality", "integrity"),
            checks=(
                _check("quality", checker=CHECKER_B, evidence=EVIDENCE_B),
                _check("integrity", checker=CHECKER_A, evidence=EVIDENCE_A),
            ),
        )


def test_canonical_json_round_trip_binds_trusted_digest() -> None:
    receipt = _verified_receipt()
    raw = receipt.to_json()

    restored = CandidateDatasetVerification.from_json(
        raw,
        expected_receipt_sha256=receipt.receipt_sha256,
    )

    assert restored == receipt
    assert restored.verification_sha256 == receipt.verification_sha256


def test_round_trip_preserves_failed_receipt_without_promoting_it() -> None:
    receipt = CandidateDatasetVerification.create(
        candidate_material_sha256=MATERIAL,
        verification_policy_sha256=POLICY,
        required_check_ids=("integrity",),
        checks=(
            _check(
                "integrity",
                checker=CHECKER_A,
                evidence=EVIDENCE_A,
                outcome=VerificationOutcome.FAIL,
            ),
        ),
    )

    restored = CandidateDatasetVerification.from_json(receipt.to_json())

    assert restored == receipt
    assert restored.is_verified is False
    with pytest.raises(LearningVerificationRejectedError):
        _ = restored.verification_sha256


def test_tampered_payload_digest_is_rejected() -> None:
    receipt = _verified_receipt()
    payload = json.loads(receipt.to_json())
    payload["receipt"]["verification_policy_sha256"] = "a" * 64

    with pytest.raises(
        LearningVerificationIntegrityError,
        match="receipt digest mismatch",
    ):
        CandidateDatasetVerification.from_json(
            json.dumps(payload, separators=(",", ":"), sort_keys=True)
        )


def test_duplicate_json_key_is_rejected() -> None:
    raw = _verified_receipt().to_json()
    duplicate = raw[:-1] + ',"receipt_sha256":"' + "0" * 64 + '"}'

    with pytest.raises(
        LearningVerificationIntegrityError,
        match="duplicate JSON key",
    ):
        CandidateDatasetVerification.from_json(duplicate)


def test_noncanonical_serialization_is_rejected() -> None:
    raw = _verified_receipt().to_json()
    parsed = json.loads(raw)
    noncanonical = json.dumps(parsed, ensure_ascii=False, indent=2, sort_keys=True)

    with pytest.raises(
        LearningVerificationIntegrityError,
        match="serialization is not canonical",
    ):
        CandidateDatasetVerification.from_json(noncanonical)


def test_extra_receipt_key_is_rejected_even_with_recomputed_digest() -> None:
    receipt = _verified_receipt()
    envelope = json.loads(receipt.to_json())
    envelope["receipt"]["unexpected"] = "value"
    canonical_receipt = json.dumps(
        envelope["receipt"],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    envelope["receipt_sha256"] = hashlib.sha256(canonical_receipt).hexdigest()
    raw = json.dumps(
        envelope,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    with pytest.raises(
        LearningVerificationIntegrityError,
        match="receipt keys are invalid",
    ):
        CandidateDatasetVerification.from_json(raw)


def test_expected_digest_mismatch_fails_closed() -> None:
    with pytest.raises(
        LearningVerificationIntegrityError,
        match="trusted learning-verification digest mismatch",
    ):
        CandidateDatasetVerification.from_json(
            _verified_receipt().to_json(),
            expected_receipt_sha256="f" * 64,
        )


def test_invalid_utf8_bytes_fail_closed() -> None:
    with pytest.raises(
        LearningVerificationIntegrityError,
        match="not valid UTF-8",
    ):
        CandidateDatasetVerification.from_json(b"\xff")


def test_empty_required_check_set_is_rejected() -> None:
    with pytest.raises(
        LearningVerificationValidationError,
        match="required check count",
    ):
        CandidateDatasetVerification.create(
            candidate_material_sha256=MATERIAL,
            verification_policy_sha256=POLICY,
            required_check_ids=(),
            checks=(),
        )
