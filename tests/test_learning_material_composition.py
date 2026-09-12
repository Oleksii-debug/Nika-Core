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
) -> CandidateDatasetVerification:
    evidence = VerificationCheckEvidence(
        check_id="integrity",
        checker_sha256=I,
        evidence_sha256=J,
        outcome=outcome,
    )
    return CandidateDatasetVerification.create(
        candidate_material_sha256=material_sha256,
        verification_policy_sha256=K,
        required_check_ids=("integrity",),
        checks=(evidence,),
    )


def _freeze(
    verification: CandidateDatasetVerification,
    *,
    shards: tuple[LearningShard, ...] | None = None,
    selection_policy_sha256: str = H,
):
    return freeze_verified_learning_package(
        package_id="candidate",
        package_version="v1",
        base_artifact_sha256=G,
        selection_policy_sha256=selection_policy_sha256,
        evaluation_set_sha256=K,
        shards=_shards() if shards is None else shards,
        verification=verification,
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
