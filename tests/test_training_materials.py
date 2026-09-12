from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from nika_core.learning_package import FrozenLearningPackage, LearningDataSplit, LearningShard
from nika_core.research.blobs import BlobStoreError, ContentAddressedBlobStore
from nika_core.training_materials import (
    ResolvedTrainingMaterial,
    ResolvedTrainingPackage,
    TrainingMaterialEvidence,
    TrainingMaterialResolutionError,
    TrainingMaterialSetEvidence,
    resolve_training_materials,
)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _shard(
    *,
    split: LearningDataSplit,
    payload: bytes,
    provenance: bytes,
    license_evidence: bytes,
    byte_count: int | None = None,
) -> LearningShard:
    return LearningShard(
        split=split,
        artifact_sha256=_sha256(payload),
        provenance_sha256=_sha256(provenance),
        license_evidence_sha256=_sha256(license_evidence),
        record_count=2,
        byte_count=len(payload) if byte_count is None else byte_count,
    )


def _package(
    training: LearningShard,
    validation: LearningShard,
    *,
    selection_policy_sha256: str | None = None,
) -> FrozenLearningPackage:
    return FrozenLearningPackage.freeze(
        package_id="pkg-1",
        package_version="1",
        base_artifact_sha256=_sha256(b"base-model"),
        selection_policy_sha256=(
            selection_policy_sha256
            if selection_policy_sha256 is not None
            else _sha256(b"selection-policy")
        ),
        verification_sha256=_sha256(b"verification-receipt"),
        evaluation_set_sha256=_sha256(b"strict-held-out-evaluation"),
        shards=(validation, training),
    )


def _stored_package(
    tmp_path: Path,
    *,
    workspace_id: str = "workspace-alpha",
) -> tuple[ContentAddressedBlobStore, FrozenLearningPackage, bytes, bytes]:
    store = ContentAddressedBlobStore(tmp_path / "blob-store")
    training_body = b'{"prompt":"a","response":"b"}\n'
    validation_body = b'{"prompt":"c","response":"d"}\n'
    store.put_bytes(workspace_id, training_body)
    store.put_bytes(workspace_id, validation_body)
    training = _shard(
        split=LearningDataSplit.TRAINING,
        payload=training_body,
        provenance=b"training-provenance",
        license_evidence=b"training-license",
    )
    validation = _shard(
        split=LearningDataSplit.VALIDATION,
        payload=validation_body,
        provenance=b"validation-provenance",
        license_evidence=b"validation-license",
    )
    return store, _package(training, validation), training_body, validation_body


def _material(
    *,
    split: LearningDataSplit,
    artifact_sha256: str,
    byte_count: int = 7,
) -> TrainingMaterialEvidence:
    return TrainingMaterialEvidence(
        split=split,
        artifact_sha256=artifact_sha256,
        provenance_sha256=_sha256(b"provenance" + artifact_sha256.encode()),
        license_evidence_sha256=_sha256(b"license" + artifact_sha256.encode()),
        record_count=1,
        byte_count=byte_count,
    )


def _material_set(
    training: TrainingMaterialEvidence,
    validation: TrainingMaterialEvidence,
    *,
    evaluation_set_sha256: str | None = None,
    candidate_dataset_sha256: str | None = None,
    package_manifest_sha256: str | None = None,
) -> TrainingMaterialSetEvidence:
    package = FrozenLearningPackage.freeze(
        package_id="pkg-evidence",
        package_version="1",
        base_artifact_sha256=_sha256(b"base-model"),
        selection_policy_sha256=_sha256(b"selection-policy"),
        verification_sha256=_sha256(b"verification"),
        evaluation_set_sha256=(
            evaluation_set_sha256
            if evaluation_set_sha256 is not None
            else _sha256(b"held-out")
        ),
        shards=(training.to_learning_shard(), validation.to_learning_shard()),
    )
    canonical_materials = tuple(
        TrainingMaterialEvidence.from_shard(shard) for shard in package.shards
    )
    return TrainingMaterialSetEvidence(
        workspace_sha256=_sha256(b"workspace-alpha"),
        package_id=package.package_id,
        package_version=package.package_version,
        package_schema_version=package.schema_version,
        base_artifact_sha256=package.base_artifact_sha256,
        selection_policy_sha256=package.selection_policy_sha256,
        verification_sha256=package.verification_sha256,
        evaluation_set_sha256=package.evaluation_set_sha256,
        candidate_dataset_sha256=(
            candidate_dataset_sha256
            if candidate_dataset_sha256 is not None
            else package.candidate_dataset_sha256
        ),
        package_manifest_sha256=(
            package_manifest_sha256
            if package_manifest_sha256 is not None
            else package.manifest_sha256
        ),
        materials=canonical_materials,
    )


def test_resolves_exact_frozen_shard_bytes_and_minimizes_durable_evidence(
    tmp_path: Path,
) -> None:
    workspace_id = "workspace-alpha"
    store, package, _, _ = _stored_package(tmp_path, workspace_id=workspace_id)

    resolved = resolve_training_materials(
        package,
        workspace_id=workspace_id,
        blob_store=store,
    )

    assert len(resolved.materials) == 2
    assert all(material.path.is_absolute() for material in resolved.materials)
    assert all(material.path.is_file() for material in resolved.materials)
    assert resolved.evidence.package_manifest_sha256 == package.manifest_sha256
    assert resolved.evidence.candidate_dataset_sha256 == package.candidate_dataset_sha256
    assert resolved.evidence.base_artifact_sha256 == package.base_artifact_sha256
    assert resolved.evidence.selection_policy_sha256 == package.selection_policy_sha256
    assert resolved.evidence.verification_sha256 == package.verification_sha256
    assert resolved.evidence.evaluation_set_sha256 == package.evaluation_set_sha256
    assert len(resolved.training_material_sha256) == 64
    durable = resolved.evidence.canonical_payload()
    assert "path" not in repr(durable)
    assert workspace_id not in repr(durable)


def test_workspace_isolation_blocks_same_digest_from_another_workspace(
    tmp_path: Path,
) -> None:
    store, package, _, _ = _stored_package(tmp_path, workspace_id="workspace-alpha")

    with pytest.raises(TrainingMaterialResolutionError, match="unavailable or inconsistent"):
        resolve_training_materials(
            package,
            workspace_id="workspace-beta",
            blob_store=store,
        )


def test_missing_frozen_shard_fails_closed(tmp_path: Path) -> None:
    store, package, _, _ = _stored_package(tmp_path)
    first = package.shards[0]
    stored_path = store.resolve_digest("workspace-alpha", first.artifact_sha256, first.byte_count)
    stored_path.unlink()

    with pytest.raises(TrainingMaterialResolutionError, match="unavailable or inconsistent"):
        resolve_training_materials(
            package,
            workspace_id="workspace-alpha",
            blob_store=store,
        )


def test_same_size_tamper_is_detected_on_resume_reresolution(tmp_path: Path) -> None:
    store, package, _, _ = _stored_package(tmp_path)
    first_resolution = resolve_training_materials(
        package,
        workspace_id="workspace-alpha",
        blob_store=store,
    )
    target = first_resolution.materials[0]
    original = target.path.read_bytes()
    target.path.write_bytes(b"x" * len(original))

    with pytest.raises(TrainingMaterialResolutionError, match="unavailable or inconsistent"):
        resolve_training_materials(
            package,
            workspace_id="workspace-alpha",
            blob_store=store,
        )


def test_shard_byte_count_must_match_storage_identity(tmp_path: Path) -> None:
    store = ContentAddressedBlobStore(tmp_path / "blob-store")
    training_body = b"training"
    validation_body = b"validation"
    store.put_bytes("workspace-alpha", training_body)
    store.put_bytes("workspace-alpha", validation_body)
    training = _shard(
        split=LearningDataSplit.TRAINING,
        payload=training_body,
        provenance=b"p1",
        license_evidence=b"l1",
        byte_count=len(training_body) + 1,
    )
    validation = _shard(
        split=LearningDataSplit.VALIDATION,
        payload=validation_body,
        provenance=b"p2",
        license_evidence=b"l2",
    )

    with pytest.raises(TrainingMaterialResolutionError, match="unavailable or inconsistent"):
        resolve_training_materials(
            _package(training, validation),
            workspace_id="workspace-alpha",
            blob_store=store,
        )


def test_held_out_evaluation_digest_is_never_resolved_as_training_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, package, _, _ = _stored_package(tmp_path)
    calls: list[str] = []
    original_resolve = store.resolve_digest

    def tracking_resolve(workspace_id: str, raw_sha256: str, byte_size: int) -> Path:
        calls.append(raw_sha256)
        return original_resolve(workspace_id, raw_sha256, byte_size)

    monkeypatch.setattr(store, "resolve_digest", tracking_resolve)

    resolved = resolve_training_materials(
        package,
        workspace_id="workspace-alpha",
        blob_store=store,
    )

    assert calls == [shard.artifact_sha256 for shard in package.shards]
    assert package.evaluation_set_sha256 not in calls
    assert resolved.evidence.evaluation_set_sha256 == package.evaluation_set_sha256


def test_provenance_or_license_change_changes_training_material_identity(
    tmp_path: Path,
) -> None:
    store, package, training_body, validation_body = _stored_package(tmp_path)
    baseline = resolve_training_materials(
        package,
        workspace_id="workspace-alpha",
        blob_store=store,
    )
    training = _shard(
        split=LearningDataSplit.TRAINING,
        payload=training_body,
        provenance=b"changed-training-provenance",
        license_evidence=b"training-license",
    )
    validation = _shard(
        split=LearningDataSplit.VALIDATION,
        payload=validation_body,
        provenance=b"validation-provenance",
        license_evidence=b"validation-license",
    )
    changed_package = _package(training, validation)
    changed = resolve_training_materials(
        changed_package,
        workspace_id="workspace-alpha",
        blob_store=store,
    )

    assert baseline.training_material_sha256 != changed.training_material_sha256


def test_selection_policy_change_changes_material_set_identity(tmp_path: Path) -> None:
    store, package, _, _ = _stored_package(tmp_path)
    baseline = resolve_training_materials(
        package,
        workspace_id="workspace-alpha",
        blob_store=store,
    )
    changed_package = FrozenLearningPackage.freeze(
        package_id=package.package_id,
        package_version=package.package_version,
        base_artifact_sha256=package.base_artifact_sha256,
        selection_policy_sha256=_sha256(b"different-selection-policy"),
        verification_sha256=package.verification_sha256,
        evaluation_set_sha256=package.evaluation_set_sha256,
        shards=package.shards,
    )
    changed = resolve_training_materials(
        changed_package,
        workspace_id="workspace-alpha",
        blob_store=store,
    )

    assert baseline.training_material_sha256 != changed.training_material_sha256


def test_package_and_store_must_be_canonical_exact_types(tmp_path: Path) -> None:
    store, package, _, _ = _stored_package(tmp_path)

    with pytest.raises(TypeError, match="FrozenLearningPackage"):
        resolve_training_materials(
            object(),  # type: ignore[arg-type]
            workspace_id="workspace-alpha",
            blob_store=store,
        )
    with pytest.raises(TypeError, match="canonical ContentAddressedBlobStore"):
        resolve_training_materials(
            package,
            workspace_id="workspace-alpha",
            blob_store=object(),  # type: ignore[arg-type]
        )


@pytest.mark.parametrize(
    "workspace_id",
    ["", " workspace-alpha", "workspace-alpha ", "workspace\nalpha"],
)
def test_workspace_identity_is_strict(tmp_path: Path, workspace_id: str) -> None:
    store, package, _, _ = _stored_package(tmp_path)

    with pytest.raises(ValueError):
        resolve_training_materials(
            package,
            workspace_id=workspace_id,
            blob_store=store,
        )


def test_blob_store_resolve_digest_validates_canonical_identity(tmp_path: Path) -> None:
    store = ContentAddressedBlobStore(tmp_path / "blob-store")
    artifact = store.put_bytes("workspace-alpha", b"payload")

    assert store.resolve_digest(
        "workspace-alpha",
        artifact.raw_sha256,
        artifact.byte_size,
    ).read_bytes() == b"payload"

    with pytest.raises(ValueError, match="lowercase SHA-256"):
        store.resolve_digest("workspace-alpha", "A" * 64, artifact.byte_size)
    with pytest.raises(ValueError, match="signed 64-bit"):
        store.resolve_digest("workspace-alpha", artifact.raw_sha256, -1)
    with pytest.raises(BlobStoreError, match="size does not match"):
        store.resolve_digest("workspace-alpha", artifact.raw_sha256, artifact.byte_size + 1)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("artifact_sha256", "A" * 64),
        ("provenance_sha256", "x"),
        ("license_evidence_sha256", object()),
        ("record_count", 0),
        ("record_count", True),
        ("byte_count", 0),
    ],
)
def test_material_evidence_rejects_forged_authority_fields(
    field: str,
    value: object,
) -> None:
    values: dict[str, object] = {
        "split": LearningDataSplit.TRAINING,
        "artifact_sha256": _sha256(b"artifact"),
        "provenance_sha256": _sha256(b"provenance"),
        "license_evidence_sha256": _sha256(b"license"),
        "record_count": 1,
        "byte_count": 1,
    }
    values[field] = value

    with pytest.raises((TypeError, ValueError)):
        TrainingMaterialEvidence(**values)  # type: ignore[arg-type]


def test_material_set_rejects_duplicate_artifact_identity() -> None:
    digest = _sha256(b"same-artifact")
    training = _material(split=LearningDataSplit.TRAINING, artifact_sha256=digest)
    validation = _material(split=LearningDataSplit.VALIDATION, artifact_sha256=digest)

    with pytest.raises(ValueError, match="must be unique"):
        _material_set(training, validation)


def test_material_set_rejects_held_out_as_training_input() -> None:
    training = _material(
        split=LearningDataSplit.TRAINING,
        artifact_sha256=_sha256(b"training"),
    )
    validation = _material(
        split=LearningDataSplit.VALIDATION,
        artifact_sha256=_sha256(b"validation"),
    )

    with pytest.raises(ValueError, match="held-out"):
        _material_set(
            training,
            validation,
            evaluation_set_sha256=training.artifact_sha256,
        )


def test_material_set_requires_training_and_validation() -> None:
    first = _material(
        split=LearningDataSplit.TRAINING,
        artifact_sha256=_sha256(b"one"),
    )
    second = _material(
        split=LearningDataSplit.TRAINING,
        artifact_sha256=_sha256(b"two"),
    )

    with pytest.raises(ValueError, match="validation"):
        _material_set(first, second)


def test_material_set_rejects_forged_candidate_dataset_digest() -> None:
    training = _material(
        split=LearningDataSplit.TRAINING,
        artifact_sha256=_sha256(b"training"),
    )
    validation = _material(
        split=LearningDataSplit.VALIDATION,
        artifact_sha256=_sha256(b"validation"),
    )

    with pytest.raises(ValueError, match="candidate dataset digest"):
        _material_set(
            training,
            validation,
            candidate_dataset_sha256=_sha256(b"forged-dataset"),
        )


def test_material_set_rejects_forged_package_manifest_digest() -> None:
    training = _material(
        split=LearningDataSplit.TRAINING,
        artifact_sha256=_sha256(b"training"),
    )
    validation = _material(
        split=LearningDataSplit.VALIDATION,
        artifact_sha256=_sha256(b"validation"),
    )

    with pytest.raises(ValueError, match="package manifest digest"):
        _material_set(
            training,
            validation,
            package_manifest_sha256=_sha256(b"forged-manifest"),
        )


def test_resolved_package_rejects_path_list_not_bound_to_evidence(tmp_path: Path) -> None:
    training = _material(
        split=LearningDataSplit.TRAINING,
        artifact_sha256=_sha256(b"training"),
    )
    validation = _material(
        split=LearningDataSplit.VALIDATION,
        artifact_sha256=_sha256(b"validation"),
    )
    evidence = _material_set(training, validation)
    path = (tmp_path / "candidate.bin").resolve()
    path.write_bytes(b"candidate")
    forged_materials = (
        ResolvedTrainingMaterial(evidence=validation, path=path),
        ResolvedTrainingMaterial(evidence=training, path=path),
    )

    with pytest.raises(ValueError, match="do not match"):
        ResolvedTrainingPackage(evidence=evidence, materials=forged_materials)


def test_resolved_material_requires_absolute_path() -> None:
    material = _material(
        split=LearningDataSplit.TRAINING,
        artifact_sha256=_sha256(b"training"),
    )

    with pytest.raises(ValueError, match="absolute Path"):
        ResolvedTrainingMaterial(evidence=material, path=Path("relative.bin"))
