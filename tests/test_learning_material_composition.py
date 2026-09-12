from __future__ import annotations

import pytest

from nika_core.learning_material_composition import (
    LearningMaterialCompositionError,
    candidate_material_sha256,
    freeze_verified_learning_package,
)
from nika_core.learning_package import LearningDataSplit, LearningShard
from nika_core.learning_verification import (
    CandidateDatasetVerification,
    LearningVerificationRejectedError,
    VerificationCheckEvidence,
    VerificationOutcome,
)

A = "a" * 64
B = "b" * 64
C = "c" * 64
D = "d" * 64
E = "e" * 64
F = "f" * 64
G = "0" * 64
H = "1" * 64
I = "2" * 64
J = "3" * 64
K = "4" * 64
L = "5" * 64


def _shard(
    split: LearningDataSplit,
    artifact: str,
    provenance: str,
    license_evidence: str,
) -> LearningShard:
    return LearningShard(
        split=split,
        artifact_sha256=artifact,
        provenance_sha256=provenance,
        license_evidence_sha256=license_evidence,
        record_count=10,
        byte_count=100,
    )


def _shards() -> tuple[LearningShard, ...]:
    return (
        _shard(LearningDataSplit.TRAINING, A, C, E),
        _shard(LearningDataSplit.VALIDATION, B, D, F),
    )


def _receipt(
    material_sha256: str,
    *,
    outcome: VerificationOutcome = VerificationOutcome.PASS,
    verification_policy_sha256: str = K,
    check_id: str = "integrity",
    checker_sha256: str = I,
) -> CandidateDatasetVerification:
    evidence = VerificationCheckEvidence(
        check_id=check_id,
        checker_sha256=checker_sha256,
        evidence_sha256=J,
        outcome=outcome,
    )
    return CandidateDatasetVerification.create(
        candidate_material_sha256=material_sha256,
        verification_policy_sha256=verification_policy_sha256,
        required_check_ids=(check_id,),
        checks=(evidence,),
    )


def _freeze(
    verification: CandidateDatasetVerification,
    *,
    shards: tuple[LearningShard, ...] | None = None,
    selection_policy_sha256: str = H,
    expected_verification_policy_sha256: str = K,
    expected_required_checkers: tuple[tuple[str, str], ...] = (("integrity", I),),
):
    return freeze_verified_learning_package(
        package_id="candidate",
        package_version="v1",
        base_artifact_sha256=G,
        selection_policy_sha256=selection_policy_sha256,
        evaluation_set_sha256=L,
        shards=_shards() if shards is None else shards,
        verification=verification,
        expected_verification_policy_sha256=expected_verification_policy_sha256,
        expected_required_checkers=expected_required_checkers,
    )


def test_candidate_material_identity_is_order_independent() -> None:
    shards = _shards()

    first = candidate_material_sha256(
        selection_policy_sha256=H,
        shards=shards,
    )
    second = candidate_material_sha256(
        selection_policy_sha256=H,
        shards=tuple(reversed(shards)),
    )

    assert first == second


def test_verified_receipt_freezes_exact_material_and_binds_receipt_digest() -> None:
    shards = _shards()
    material_sha256 = candidate_material_sha256(
        selection_policy_sha256=H,
        shards=shards,
    )
    receipt = _receipt(material_sha256)

    package = _freeze(receipt, shards=shards)

    assert package.verification_sha256 == receipt.verification_sha256
    assert package.selection_policy_sha256 == H
    assert package.shards == tuple(sorted(shards, key=lambda item: item.split.value))
    assert (
        candidate_material_sha256(
            selection_policy_sha256=package.selection_policy_sha256,
            shards=package.shards,
        )
        == receipt.candidate_material_sha256
    )


def test_receipt_for_material_a_cannot_freeze_material_b() -> None:
    material_a = candidate_material_sha256(
        selection_policy_sha256=H,
        shards=_shards(),
    )
    receipt_a = _receipt(material_a)
    material_b = (
        _shard(LearningDataSplit.TRAINING, A, C, E),
        _shard(LearningDataSplit.VALIDATION, B, D, I),
    )

    with pytest.raises(
        LearningMaterialCompositionError,
        match="does not match frozen material",
    ):
        _freeze(receipt_a, shards=material_b)


def test_receipt_cannot_be_reused_under_different_selection_policy() -> None:
    material = candidate_material_sha256(
        selection_policy_sha256=H,
        shards=_shards(),
    )
    receipt = _receipt(material)

    with pytest.raises(
        LearningMaterialCompositionError,
        match="does not match frozen material",
    ):
        _freeze(receipt, selection_policy_sha256=I)


def test_failed_receipt_cannot_freeze_matching_material() -> None:
    material = candidate_material_sha256(
        selection_policy_sha256=H,
        shards=_shards(),
    )
    receipt = _receipt(material, outcome=VerificationOutcome.FAIL)

    with pytest.raises(LearningVerificationRejectedError):
        _freeze(receipt)


def test_composition_rejects_non_tuple_shard_ingress_before_canonicalization() -> None:
    shards = _shards()

    with pytest.raises(
        LearningMaterialCompositionError,
        match="immutable tuple",
    ):
        candidate_material_sha256(
            selection_policy_sha256=H,
            shards=list(shards),  # type: ignore[arg-type]
        )


def test_composition_rejects_receipt_subclass_before_trust() -> None:
    material = candidate_material_sha256(
        selection_policy_sha256=H,
        shards=_shards(),
    )

    class _ReceiptSubclass(CandidateDatasetVerification):
        pass

    receipt = _ReceiptSubclass.create(
        candidate_material_sha256=material,
        verification_policy_sha256=K,
        required_check_ids=("integrity",),
        checks=(
            VerificationCheckEvidence(
                check_id="integrity",
                checker_sha256=I,
                evidence_sha256=J,
                outcome=VerificationOutcome.PASS,
            ),
        ),
    )

    with pytest.raises(
        LearningMaterialCompositionError,
        match="exact CandidateDatasetVerification",
    ):
        _freeze(receipt)


def test_caller_selected_verification_policy_cannot_authorize_freeze() -> None:
    material = candidate_material_sha256(
        selection_policy_sha256=H,
        shards=_shards(),
    )
    receipt = _receipt(
        material,
        verification_policy_sha256=J,
    )

    with pytest.raises(
        LearningMaterialCompositionError,
        match="verification policy does not match",
    ):
        _freeze(receipt)


def test_caller_selected_checker_cannot_authorize_freeze() -> None:
    material = candidate_material_sha256(
        selection_policy_sha256=H,
        shards=_shards(),
    )
    receipt = _receipt(
        material,
        checker_sha256=J,
    )

    with pytest.raises(
        LearningMaterialCompositionError,
        match="checker authority does not match",
    ):
        _freeze(receipt)


def test_spoofable_receipt_sha_string_is_rejected_before_comparison() -> None:
    class _ForgedString(str):
        def __eq__(self, other: object) -> bool:
            return True

        def __ne__(self, other: object) -> bool:
            return False

    material = candidate_material_sha256(
        selection_policy_sha256=H,
        shards=_shards(),
    )
    receipt = _receipt(_ForgedString(A))

    with pytest.raises(
        LearningMaterialCompositionError,
        match="candidate_material_sha256 must be an exact",
    ):
        _freeze(receipt)


def test_spoofable_shard_sha_string_is_rejected_before_canonicalization() -> None:
    class _ForgedString(str):
        pass

    forged = LearningShard(
        split=LearningDataSplit.TRAINING,
        artifact_sha256=_ForgedString(A),
        provenance_sha256=C,
        license_evidence_sha256=E,
        record_count=10,
        byte_count=100,
    )
    shards = (
        forged,
        _shard(LearningDataSplit.VALIDATION, B, D, F),
    )

    with pytest.raises(
        LearningMaterialCompositionError,
        match="shard artifact_sha256 must be an exact",
    ):
        candidate_material_sha256(
            selection_policy_sha256=H,
            shards=shards,
        )


def test_expected_checker_authority_must_be_exact_and_bounded() -> None:
    material = candidate_material_sha256(
        selection_policy_sha256=H,
        shards=_shards(),
    )
    receipt = _receipt(material)

    with pytest.raises(
        LearningMaterialCompositionError,
        match="immutable tuple",
    ):
        _freeze(
            receipt,
            expected_required_checkers=[("integrity", I)],  # type: ignore[arg-type]
        )
