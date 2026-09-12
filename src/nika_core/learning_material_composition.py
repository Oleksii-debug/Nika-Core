from __future__ import annotations

import hashlib
import json
import re

from nika_core.learning_package import FrozenLearningPackage, LearningShard
from nika_core.learning_verification import CandidateDatasetVerification

_MATERIAL_SCHEMA_VERSION = 1
_MAX_SHARDS = 1024
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class LearningMaterialCompositionError(ValueError):
    """Raised when verified Loop-C material cannot be frozen safely."""


def _require_sha256(value: object, *, field: str) -> str:
    if type(value) is not str or not _SHA256_RE.fullmatch(value):
        raise LearningMaterialCompositionError(
            f"{field} must be an exact lowercase SHA-256 string"
        )
    return value


def _canonical_json_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _canonical_shards(shards: tuple[LearningShard, ...]) -> tuple[LearningShard, ...]:
    if type(shards) is not tuple:
        raise LearningMaterialCompositionError("shards must be an immutable tuple")
    if not 1 <= len(shards) <= _MAX_SHARDS:
        raise LearningMaterialCompositionError(
            "shard count is outside the supported bound"
        )
    if not all(type(shard) is LearningShard for shard in shards):
        raise LearningMaterialCompositionError(
            "shards must contain exact LearningShard values"
        )
    return tuple(
        sorted(
            shards,
            key=lambda shard: (
                shard.split.value,
                shard.artifact_sha256,
                shard.provenance_sha256,
                shard.license_evidence_sha256,
            ),
        )
    )


def candidate_material_sha256(
    *,
    selection_policy_sha256: str,
    shards: tuple[LearningShard, ...],
) -> str:
    """Return the deterministic pre-verification identity for Loop-C material."""

    selection_policy = _require_sha256(
        selection_policy_sha256,
        field="selection_policy_sha256",
    )
    canonical_shards = _canonical_shards(shards)
    payload = {
        "schema_version": _MATERIAL_SCHEMA_VERSION,
        "selection_policy_sha256": selection_policy,
        "shards": [shard.canonical_payload() for shard in canonical_shards],
    }
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def freeze_verified_learning_package(
    *,
    package_id: str,
    package_version: str,
    base_artifact_sha256: str,
    selection_policy_sha256: str,
    evaluation_set_sha256: str,
    shards: tuple[LearningShard, ...],
    verification: CandidateDatasetVerification,
) -> FrozenLearningPackage:
    """Freeze only the exact candidate material proven by a PASS receipt."""

    if type(verification) is not CandidateDatasetVerification:
        raise LearningMaterialCompositionError(
            "verification must be an exact CandidateDatasetVerification"
        )

    canonical_shards = _canonical_shards(shards)
    material_sha256 = candidate_material_sha256(
        selection_policy_sha256=selection_policy_sha256,
        shards=canonical_shards,
    )
    if verification.candidate_material_sha256 != material_sha256:
        raise LearningMaterialCompositionError(
            "verification receipt candidate material does not match frozen material"
        )

    verification_sha256 = verification.verification_sha256
    return FrozenLearningPackage.freeze(
        package_id=package_id,
        package_version=package_version,
        base_artifact_sha256=base_artifact_sha256,
        selection_policy_sha256=selection_policy_sha256,
        verification_sha256=verification_sha256,
        evaluation_set_sha256=evaluation_set_sha256,
        shards=canonical_shards,
    )
