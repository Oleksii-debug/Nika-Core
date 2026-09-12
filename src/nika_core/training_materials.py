from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from nika_core.learning_package import FrozenLearningPackage, LearningDataSplit, LearningShard
from nika_core.research.blobs import BlobStoreError, ContentAddressedBlobStore

_SCHEMA_VERSION = 1
_MAX_WORKSPACE_BYTES = 4096


class TrainingMaterialResolutionError(RuntimeError):
    """Frozen learning material could not be resolved to exact physical bytes."""


def _sha256_payload(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _workspace_fingerprint(workspace_id: str) -> str:
    if type(workspace_id) is not str:
        raise TypeError("workspace_id must be text")
    if not workspace_id or workspace_id != workspace_id.strip():
        raise ValueError("workspace_id must be non-empty without surrounding whitespace")
    if any(ord(character) < 32 or ord(character) == 127 for character in workspace_id):
        raise ValueError("workspace_id must not contain control characters")
    encoded = workspace_id.encode("utf-8")
    if len(encoded) > _MAX_WORKSPACE_BYTES:
        raise ValueError("workspace_id exceeds the configured byte limit")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class TrainingMaterialEvidence:
    """Secret-minimized binding for one resolved frozen learning shard."""

    split: LearningDataSplit
    artifact_sha256: str
    provenance_sha256: str
    license_evidence_sha256: str
    record_count: int
    byte_count: int

    @classmethod
    def from_shard(cls, shard: LearningShard) -> TrainingMaterialEvidence:
        if type(shard) is not LearningShard:
            raise TypeError("shard must be an exact LearningShard")
        return cls(
            split=shard.split,
            artifact_sha256=shard.artifact_sha256,
            provenance_sha256=shard.provenance_sha256,
            license_evidence_sha256=shard.license_evidence_sha256,
            record_count=shard.record_count,
            byte_count=shard.byte_count,
        )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "artifact_sha256": self.artifact_sha256,
            "byte_count": self.byte_count,
            "license_evidence_sha256": self.license_evidence_sha256,
            "provenance_sha256": self.provenance_sha256,
            "record_count": self.record_count,
            "split": self.split.value,
        }


@dataclass(frozen=True, slots=True)
class TrainingMaterialSetEvidence:
    """Durable-safe evidence for the exact bytes resolved toward one training job."""

    workspace_sha256: str
    package_manifest_sha256: str
    candidate_dataset_sha256: str
    evaluation_set_sha256: str
    materials: tuple[TrainingMaterialEvidence, ...]
    schema_version: int = _SCHEMA_VERSION

    def canonical_payload(self) -> dict[str, object]:
        return {
            "candidate_dataset_sha256": self.candidate_dataset_sha256,
            "evaluation_set_sha256": self.evaluation_set_sha256,
            "materials": [material.canonical_payload() for material in self.materials],
            "package_manifest_sha256": self.package_manifest_sha256,
            "schema_version": self.schema_version,
            "workspace_sha256": self.workspace_sha256,
        }

    @property
    def training_material_sha256(self) -> str:
        return _sha256_payload(self.canonical_payload())


@dataclass(frozen=True, slots=True)
class ResolvedTrainingMaterial:
    """Transient execution-only path paired with durable-safe shard evidence."""

    evidence: TrainingMaterialEvidence
    path: Path


@dataclass(frozen=True, slots=True)
class ResolvedTrainingPackage:
    """Transient resolved paths plus durable-safe exact material-set evidence."""

    evidence: TrainingMaterialSetEvidence
    materials: tuple[ResolvedTrainingMaterial, ...]

    @property
    def training_material_sha256(self) -> str:
        return self.evidence.training_material_sha256


def resolve_training_materials(
    package: FrozenLearningPackage,
    *,
    workspace_id: str,
    blob_store: ContentAddressedBlobStore,
) -> ResolvedTrainingPackage:
    """Re-resolve and reverify every frozen TRAINING/VALIDATION shard.

    This function is intentionally effect-free beyond bounded filesystem reads. A caller
    must invoke it again immediately before every initial or resumed trainer effect; a
    previous resolution is evidence only and never grants replay authority.
    """
    if type(package) is not FrozenLearningPackage:
        raise TypeError("package must be an exact FrozenLearningPackage")
    if type(blob_store) is not ContentAddressedBlobStore:
        raise TypeError("blob_store must be the canonical ContentAddressedBlobStore")
    workspace_sha256 = _workspace_fingerprint(workspace_id)

    resolved: list[ResolvedTrainingMaterial] = []
    evidence_items: list[TrainingMaterialEvidence] = []
    for shard in package.shards:
        if shard.split not in (LearningDataSplit.TRAINING, LearningDataSplit.VALIDATION):
            raise TrainingMaterialResolutionError(
                "frozen package contains a non-training material split"
            )
        try:
            path = blob_store.resolve_digest(
                workspace_id,
                shard.artifact_sha256,
                shard.byte_count,
            )
        except BlobStoreError as exc:
            raise TrainingMaterialResolutionError(
                "frozen learning shard bytes are unavailable or inconsistent"
            ) from exc
        item_evidence = TrainingMaterialEvidence.from_shard(shard)
        evidence_items.append(item_evidence)
        resolved.append(ResolvedTrainingMaterial(evidence=item_evidence, path=path))

    material_set_evidence = TrainingMaterialSetEvidence(
        workspace_sha256=workspace_sha256,
        package_manifest_sha256=package.manifest_sha256,
        candidate_dataset_sha256=package.candidate_dataset_sha256,
        evaluation_set_sha256=package.evaluation_set_sha256,
        materials=tuple(evidence_items),
    )
    return ResolvedTrainingPackage(
        evidence=material_set_evidence,
        materials=tuple(resolved),
    )
