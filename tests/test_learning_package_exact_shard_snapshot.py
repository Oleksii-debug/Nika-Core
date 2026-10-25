from __future__ import annotations

import pytest

from nika_core.learning_package import (
    FrozenLearningPackage,
    LearningDataSplit,
    LearningPackageValidationError,
    LearningShard,
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


def _shard(
    split: LearningDataSplit,
    artifact_sha256: str,
    provenance_sha256: str,
    license_evidence_sha256: str,
) -> LearningShard:
    return LearningShard(
        split=split,
        artifact_sha256=artifact_sha256,
        provenance_sha256=provenance_sha256,
        license_evidence_sha256=license_evidence_sha256,
        record_count=10,
        byte_count=100,
    )


def _package(
    shards: tuple[LearningShard, ...],
) -> FrozenLearningPackage:
    return FrozenLearningPackage.freeze(
        package_id="candidate",
        package_version="v1",
        base_artifact_sha256=G,
        selection_policy_sha256=H,
        verification_sha256=I,
        evaluation_set_sha256=J,
        shards=shards,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("record_count", -1),
        ("byte_count", False),
    ],
)
def test_package_revalidates_constructor_bypassed_exact_shard(
    field: str,
    value: object,
) -> None:
    forged = object.__new__(LearningShard)
    object.__setattr__(forged, "split", LearningDataSplit.TRAINING)
    object.__setattr__(forged, "artifact_sha256", A)
    object.__setattr__(forged, "provenance_sha256", C)
    object.__setattr__(forged, "license_evidence_sha256", E)
    object.__setattr__(forged, "record_count", 10)
    object.__setattr__(forged, "byte_count", 100)
    object.__setattr__(forged, field, value)
    validation = _shard(LearningDataSplit.VALIDATION, B, D, F)

    with pytest.raises(LearningPackageValidationError, match="positive signed-64"):
        _package((forged, validation))

    with pytest.raises(LearningPackageValidationError, match="positive signed-64"):
        FrozenLearningPackage(
            package_id="candidate",
            package_version="v1",
            base_artifact_sha256=G,
            selection_policy_sha256=H,
            verification_sha256=I,
            evaluation_set_sha256=J,
            shards=(forged, validation),
        )


def test_frozen_package_snapshots_exact_shards_before_digest_authority() -> None:
    training = _shard(LearningDataSplit.TRAINING, A, C, E)
    validation = _shard(LearningDataSplit.VALIDATION, B, D, F)
    package = _package((training, validation))
    original_manifest_sha256 = package.manifest_sha256
    original_json = package.to_json()

    object.__setattr__(training, "record_count", -1)
    object.__setattr__(training, "artifact_sha256", F)

    assert package.shards[0] is not training
    assert package.shards[0].record_count == 10
    assert package.shards[0].artifact_sha256 == A
    assert package.manifest_sha256 == original_manifest_sha256
    assert package.to_json() == original_json
    assert FrozenLearningPackage.from_json(original_json) == package
