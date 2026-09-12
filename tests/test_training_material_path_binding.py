from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from nika_core.learning_package import FrozenLearningPackage, LearningDataSplit
from nika_core.training_materials import (
    ResolvedTrainingMaterial,
    ResolvedTrainingPackage,
    TrainingMaterialEvidence,
    TrainingMaterialResolutionError,
    TrainingMaterialSetEvidence,
)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _material(split: LearningDataSplit, payload: bytes) -> TrainingMaterialEvidence:
    return TrainingMaterialEvidence(
        split=split,
        artifact_sha256=_sha256(payload),
        provenance_sha256=_sha256(b"provenance-" + payload),
        license_evidence_sha256=_sha256(b"license-" + payload),
        record_count=1,
        byte_count=len(payload),
    )


def _evidence(
    training: TrainingMaterialEvidence,
    validation: TrainingMaterialEvidence,
) -> TrainingMaterialSetEvidence:
    package = FrozenLearningPackage.freeze(
        package_id="path-binding",
        package_version="1",
        base_artifact_sha256=_sha256(b"base"),
        selection_policy_sha256=_sha256(b"selection"),
        verification_sha256=_sha256(b"verification"),
        evaluation_set_sha256=_sha256(b"held-out"),
        shards=(training.to_learning_shard(), validation.to_learning_shard()),
    )
    materials = tuple(
        TrainingMaterialEvidence.from_shard(shard) for shard in package.shards
    )
    return TrainingMaterialSetEvidence.from_package(
        package,
        workspace_sha256=_sha256(b"workspace-alpha"),
        materials=materials,
    )


def _resolved_materials(
    evidence: TrainingMaterialSetEvidence,
    training_path: Path,
    validation_path: Path,
) -> tuple[ResolvedTrainingMaterial, ...]:
    paths_by_split = {
        LearningDataSplit.TRAINING: training_path,
        LearningDataSplit.VALIDATION: validation_path,
    }
    return tuple(
        ResolvedTrainingMaterial(evidence=item, path=paths_by_split[item.split])
        for item in evidence.materials
    )


def test_canonical_platform_paths_bind_to_matching_bytes(tmp_path: Path) -> None:
    training_body = b"training-material"
    validation_body = b"validation-material"
    training = _material(LearningDataSplit.TRAINING, training_body)
    validation = _material(LearningDataSplit.VALIDATION, validation_body)
    evidence = _evidence(training, validation)
    training_path = (tmp_path / "training.bin").resolve()
    validation_path = (tmp_path / "validation.bin").resolve()
    training_path.write_bytes(training_body)
    validation_path.write_bytes(validation_body)

    resolved = ResolvedTrainingPackage(
        evidence=evidence,
        materials=_resolved_materials(evidence, training_path, validation_path),
    )

    assert resolved.training_material_sha256 == evidence.training_material_sha256
    resolved.reverify()


def test_same_evidence_order_with_different_bytes_is_rejected(tmp_path: Path) -> None:
    training_body = b"training-material"
    validation_body = b"validation-material"
    training = _material(LearningDataSplit.TRAINING, training_body)
    validation = _material(LearningDataSplit.VALIDATION, validation_body)
    evidence = _evidence(training, validation)
    training_path = (tmp_path / "training.bin").resolve()
    validation_path = (tmp_path / "validation.bin").resolve()
    training_path.write_bytes(b"X" * len(training_body))
    validation_path.write_bytes(validation_body)

    with pytest.raises(TrainingMaterialResolutionError, match="digest does not match"):
        ResolvedTrainingPackage(
            evidence=evidence,
            materials=_resolved_materials(evidence, training_path, validation_path),
        )


def test_reverify_detects_post_construction_same_size_tamper(tmp_path: Path) -> None:
    training_body = b"training-material"
    validation_body = b"validation-material"
    training = _material(LearningDataSplit.TRAINING, training_body)
    validation = _material(LearningDataSplit.VALIDATION, validation_body)
    evidence = _evidence(training, validation)
    training_path = (tmp_path / "training.bin").resolve()
    validation_path = (tmp_path / "validation.bin").resolve()
    training_path.write_bytes(training_body)
    validation_path.write_bytes(validation_body)
    resolved = ResolvedTrainingPackage(
        evidence=evidence,
        materials=_resolved_materials(evidence, training_path, validation_path),
    )
    training_path.write_bytes(b"Y" * len(training_body))

    with pytest.raises(TrainingMaterialResolutionError, match="digest does not match"):
        resolved.reverify()


def test_non_regular_material_path_is_rejected(tmp_path: Path) -> None:
    training_body = b"training-material"
    validation_body = b"validation-material"
    training = _material(LearningDataSplit.TRAINING, training_body)
    validation = _material(LearningDataSplit.VALIDATION, validation_body)
    evidence = _evidence(training, validation)
    validation_path = (tmp_path / "validation.bin").resolve()
    validation_path.write_bytes(validation_body)
    directory_path = (tmp_path / "training-dir").resolve()
    directory_path.mkdir()

    with pytest.raises(TrainingMaterialResolutionError, match="regular file"):
        ResolvedTrainingPackage(
            evidence=evidence,
            materials=_resolved_materials(evidence, directory_path, validation_path),
        )
