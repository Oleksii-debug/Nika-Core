from __future__ import annotations

import hashlib
import hmac
import json
import re

from nika_core.learning_package import (
    FrozenLearningPackage,
    LearningDataSplit,
    LearningShard,
)
from nika_core.learning_verification import (
    CandidateDatasetVerification,
    VerificationCheckEvidence,
    VerificationOutcome,
)

_MATERIAL_SCHEMA_VERSION = 1
_MAX_SHARDS = 1024
_MAX_CHECKS = 1024
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}\Z")


class LearningMaterialCompositionError(ValueError):
    """Raised when verified Loop-C material cannot be frozen safely."""


def _require_sha256(value: object, *, field: str) -> str:
    if type(value) is not str or not _SHA256_RE.fullmatch(value):
        raise LearningMaterialCompositionError(
            f"{field} must be an exact lowercase SHA-256 string"
        )
    return value


def _require_token(value: object, *, field: str) -> str:
    if type(value) is not str or not _TOKEN_RE.fullmatch(value):
        raise LearningMaterialCompositionError(
            f"{field} must be an exact bounded machine token"
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
    for shard in shards:
        if type(shard.split) is not LearningDataSplit:
            raise LearningMaterialCompositionError(
                "shard split must be an exact LearningDataSplit"
            )
        _require_sha256(shard.artifact_sha256, field="shard artifact_sha256")
        _require_sha256(shard.provenance_sha256, field="shard provenance_sha256")
        _require_sha256(
            shard.license_evidence_sha256,
            field="shard license_evidence_sha256",
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


def _canonical_expected_checkers(
    value: object,
) -> tuple[tuple[str, str], ...]:
    if type(value) is not tuple:
        raise LearningMaterialCompositionError(
            "expected_required_checkers must be an immutable tuple"
        )
    if not 1 <= len(value) <= _MAX_CHECKS:
        raise LearningMaterialCompositionError(
            "expected required checker count is outside the supported bound"
        )
    normalized: list[tuple[str, str]] = []
    for item in value:
        if type(item) is not tuple or len(item) != 2:
            raise LearningMaterialCompositionError(
                "expected_required_checkers entries must be exact pairs"
            )
        check_id = _require_token(item[0], field="expected check_id")
        checker_sha256 = _require_sha256(
            item[1],
            field="expected checker_sha256",
        )
        normalized.append((check_id, checker_sha256))
    normalized.sort(key=lambda item: item[0])
    check_ids = tuple(item[0] for item in normalized)
    if len(set(check_ids)) != len(check_ids):
        raise LearningMaterialCompositionError(
            "expected required checker ids must be unique"
        )
    return tuple(normalized)


def _assert_trusted_verification_authority(
    *,
    verification: CandidateDatasetVerification,
    expected_verification_policy_sha256: str,
    expected_required_checkers: tuple[tuple[str, str], ...],
) -> None:
    receipt_policy_sha256 = _require_sha256(
        verification.verification_policy_sha256,
        field="verification verification_policy_sha256",
    )
    trusted_policy_sha256 = _require_sha256(
        expected_verification_policy_sha256,
        field="expected_verification_policy_sha256",
    )
    if not hmac.compare_digest(receipt_policy_sha256, trusted_policy_sha256):
        raise LearningMaterialCompositionError(
            "verification policy does not match the trusted expectation"
        )

    if type(verification.required_check_ids) is not tuple:
        raise LearningMaterialCompositionError(
            "verification required_check_ids must be an immutable tuple"
        )
    if not 1 <= len(verification.required_check_ids) <= _MAX_CHECKS:
        raise LearningMaterialCompositionError(
            "verification required check count is outside the supported bound"
        )
    receipt_required_ids = tuple(
        _require_token(check_id, field="verification required check_id")
        for check_id in verification.required_check_ids
    )
    if receipt_required_ids != tuple(sorted(receipt_required_ids)):
        raise LearningMaterialCompositionError(
            "verification required check ids are not in canonical order"
        )
    if len(set(receipt_required_ids)) != len(receipt_required_ids):
        raise LearningMaterialCompositionError(
            "verification required check ids must be unique"
        )

    if type(verification.checks) is not tuple:
        raise LearningMaterialCompositionError(
            "verification checks must be an immutable tuple"
        )
    if not 1 <= len(verification.checks) <= _MAX_CHECKS:
        raise LearningMaterialCompositionError(
            "verification check count is outside the supported bound"
        )

    receipt_checkers: list[tuple[str, str]] = []
    for check in verification.checks:
        if type(check) is not VerificationCheckEvidence:
            raise LearningMaterialCompositionError(
                "verification checks must contain exact VerificationCheckEvidence values"
            )
        check_id = _require_token(check.check_id, field="verification check_id")
        checker_sha256 = _require_sha256(
            check.checker_sha256,
            field="verification checker_sha256",
        )
        _require_sha256(
            check.evidence_sha256,
            field="verification evidence_sha256",
        )
        if type(check.outcome) is not VerificationOutcome:
            raise LearningMaterialCompositionError(
                "verification outcome must be an exact VerificationOutcome"
            )
        receipt_checkers.append((check_id, checker_sha256))

    receipt_checker_authority = tuple(receipt_checkers)
    if tuple(item[0] for item in receipt_checker_authority) != receipt_required_ids:
        raise LearningMaterialCompositionError(
            "verification checks do not match required_check_ids"
        )

    trusted_checker_authority = _canonical_expected_checkers(
        expected_required_checkers
    )
    if receipt_checker_authority != trusted_checker_authority:
        raise LearningMaterialCompositionError(
            "verification checker authority does not match the trusted expectation"
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
    expected_verification_policy_sha256: str,
    expected_required_checkers: tuple[tuple[str, str], ...],
) -> FrozenLearningPackage:
    """Freeze only exact material proven by a PASS receipt under trusted authority."""

    if type(verification) is not CandidateDatasetVerification:
        raise LearningMaterialCompositionError(
            "verification must be an exact CandidateDatasetVerification"
        )

    _require_token(package_id, field="package_id")
    _require_token(package_version, field="package_version")
    base_artifact = _require_sha256(
        base_artifact_sha256,
        field="base_artifact_sha256",
    )
    evaluation_set = _require_sha256(
        evaluation_set_sha256,
        field="evaluation_set_sha256",
    )
    _assert_trusted_verification_authority(
        verification=verification,
        expected_verification_policy_sha256=expected_verification_policy_sha256,
        expected_required_checkers=expected_required_checkers,
    )

    canonical_shards = _canonical_shards(shards)
    material_sha256 = candidate_material_sha256(
        selection_policy_sha256=selection_policy_sha256,
        shards=canonical_shards,
    )
    receipt_material_sha256 = _require_sha256(
        verification.candidate_material_sha256,
        field="verification candidate_material_sha256",
    )
    if not hmac.compare_digest(receipt_material_sha256, material_sha256):
        raise LearningMaterialCompositionError(
            "verification receipt candidate material does not match frozen material"
        )

    verification_sha256 = verification.verification_sha256
    return FrozenLearningPackage.freeze(
        package_id=package_id,
        package_version=package_version,
        base_artifact_sha256=base_artifact,
        selection_policy_sha256=selection_policy_sha256,
        verification_sha256=verification_sha256,
        evaluation_set_sha256=evaluation_set,
        shards=canonical_shards,
    )
