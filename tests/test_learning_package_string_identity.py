from __future__ import annotations

from typing import Self

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


class _IdentitySpoof(str):
    def __new__(cls, value: str, marker: int) -> Self:
        instance = super().__new__(cls, value)
        instance.marker = marker
        return instance

    def __hash__(self) -> int:
        return hash((str(self), self.marker))

    def __eq__(self, other: object) -> bool:
        return self is other


def _valid_shards() -> tuple[LearningShard, LearningShard]:
    return (
        LearningShard(
            split=LearningDataSplit.TRAINING,
            artifact_sha256=A,
            provenance_sha256=C,
            license_evidence_sha256=E,
            record_count=10,
            byte_count=100,
        ),
        LearningShard(
            split=LearningDataSplit.VALIDATION,
            artifact_sha256=B,
            provenance_sha256=D,
            license_evidence_sha256=F,
            record_count=10,
            byte_count=100,
        ),
    )


@pytest.mark.parametrize(
    "field",
    ["artifact_sha256", "provenance_sha256", "license_evidence_sha256"],
)
def test_learning_shard_rejects_sha256_string_subclasses(field: str) -> None:
    values: dict[str, object] = {
        "split": LearningDataSplit.TRAINING,
        "artifact_sha256": A,
        "provenance_sha256": C,
        "license_evidence_sha256": E,
        "record_count": 10,
        "byte_count": 100,
    }
    values[field] = _IdentitySpoof(str(values[field]), 1)

    with pytest.raises(LearningPackageValidationError, match=field):
        LearningShard(**values)


@pytest.mark.parametrize(
    "field,value",
    [
        ("package_id", "candidate"),
        ("package_version", "v1"),
        ("base_artifact_sha256", G),
        ("selection_policy_sha256", H),
        ("verification_sha256", I),
        ("evaluation_set_sha256", J),
    ],
)
def test_frozen_package_rejects_authority_string_subclasses(
    field: str,
    value: str,
) -> None:
    values: dict[str, object] = {
        "package_id": "candidate",
        "package_version": "v1",
        "base_artifact_sha256": G,
        "selection_policy_sha256": H,
        "verification_sha256": I,
        "evaluation_set_sha256": J,
        "shards": _valid_shards(),
    }
    values[field] = _IdentitySpoof(value, 1)

    with pytest.raises(LearningPackageValidationError, match=field):
        FrozenLearningPackage.freeze(**values)


def test_duplicate_artifact_identity_cannot_use_spoofed_equality_or_hash() -> None:
    spoofed_duplicate = _IdentitySpoof(A, 2)

    with pytest.raises(LearningPackageValidationError, match="artifact_sha256"):
        LearningShard(
            split=LearningDataSplit.VALIDATION,
            artifact_sha256=spoofed_duplicate,
            provenance_sha256=D,
            license_evidence_sha256=F,
            record_count=10,
            byte_count=100,
        )


def test_held_out_identity_cannot_use_spoofed_equality_or_hash() -> None:
    training, validation = _valid_shards()
    spoofed_held_out = _IdentitySpoof(A, 3)

    with pytest.raises(LearningPackageValidationError, match="evaluation_set_sha256"):
        FrozenLearningPackage.freeze(
            package_id="candidate",
            package_version="v1",
            base_artifact_sha256=G,
            selection_policy_sha256=H,
            verification_sha256=I,
            evaluation_set_sha256=spoofed_held_out,
            shards=(training, validation),
        )
