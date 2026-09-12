from __future__ import annotations

import pytest

from nika_core.learning_package import (
    FrozenLearningPackage,
    LearningDataSplit,
    LearningPackageValidationError,
    LearningShard,
)

PROVENANCE_SHA256 = "c" * 64
LICENSE_SHA256 = "d" * 64
BASE_SHA256 = "e" * 64
POLICY_SHA256 = "f" * 64
VERIFICATION_SHA256 = "1" * 64
EVALUATION_SHA256 = "2" * 64


def _oversized_shard_stream():
    for index in range(1025):
        yield LearningShard(
            split=(
                LearningDataSplit.TRAINING
                if index % 2 == 0
                else LearningDataSplit.VALIDATION
            ),
            artifact_sha256=f"{index:064x}",
            provenance_sha256=PROVENANCE_SHA256,
            license_evidence_sha256=LICENSE_SHA256,
            record_count=1,
            byte_count=1,
        )
    raise AssertionError("freeze consumed beyond the supported shard bound")


def test_freeze_rejects_oversized_generator_before_unbounded_consumption() -> None:
    with pytest.raises(LearningPackageValidationError, match="shard count"):
        FrozenLearningPackage.freeze(
            package_id="bounded-generator",
            package_version="v1",
            base_artifact_sha256=BASE_SHA256,
            selection_policy_sha256=POLICY_SHA256,
            verification_sha256=VERIFICATION_SHA256,
            evaluation_set_sha256=EVALUATION_SHA256,
            shards=_oversized_shard_stream(),
        )
