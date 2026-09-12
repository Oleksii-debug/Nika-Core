from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

_SCHEMA_VERSION = 1
_MAX_MANIFEST_BYTES = 1024 * 1024
_MAX_SHARDS = 1024
_MAX_SIGNED_64 = (1 << 63) - 1
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}\Z")
_ENVELOPE_KEYS = frozenset({"manifest", "manifest_sha256"})
_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "package_id",
        "package_version",
        "base_artifact_sha256",
        "candidate_dataset_sha256",
        "selection_policy_sha256",
        "verification_sha256",
        "evaluation_set_sha256",
        "shards",
    }
)
_SHARD_KEYS = frozenset(
    {
        "split",
        "artifact_sha256",
        "provenance_sha256",
        "license_evidence_sha256",
        "record_count",
        "byte_count",
    }
)


class LearningPackageError(ValueError):
    """Base error for invalid frozen learning-package evidence."""


class LearningPackageValidationError(LearningPackageError):
    """Raised when caller-supplied package data violates the contract."""


class LearningPackageIntegrityError(LearningPackageError):
    """Raised when serialized package evidence is malformed or tampered."""


class LearningDataSplit(StrEnum):
    TRAINING = "training"
    VALIDATION = "validation"


def _require_token(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not _TOKEN_RE.fullmatch(value):
        raise LearningPackageValidationError(f"{field} must be a bounded machine token")
    return value


def _require_sha256(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise LearningPackageValidationError(f"{field} must be lowercase SHA-256")
    return value


def _require_positive_int(value: object, *, field: str) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_SIGNED_64:
        raise LearningPackageValidationError(f"{field} must be a positive signed-64 integer")
    return value


def _canonical_json_bytes(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest_payload(payload: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(payload)).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise LearningPackageIntegrityError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _canonical_shard_order(shards: Iterable[LearningShard]) -> tuple[LearningShard, ...]:
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


@dataclass(frozen=True, slots=True)
class LearningShard:
    split: LearningDataSplit
    artifact_sha256: str
    provenance_sha256: str
    license_evidence_sha256: str
    record_count: int
    byte_count: int

    def __post_init__(self) -> None:
        if not isinstance(self.split, LearningDataSplit):
            raise LearningPackageValidationError("split must be a LearningDataSplit")
        _require_sha256(self.artifact_sha256, field="artifact_sha256")
        _require_sha256(self.provenance_sha256, field="provenance_sha256")
        _require_sha256(
            self.license_evidence_sha256,
            field="license_evidence_sha256",
        )
        _require_positive_int(self.record_count, field="record_count")
        _require_positive_int(self.byte_count, field="byte_count")

    def canonical_payload(self) -> dict[str, object]:
        return {
            "artifact_sha256": self.artifact_sha256,
            "byte_count": self.byte_count,
            "license_evidence_sha256": self.license_evidence_sha256,
            "provenance_sha256": self.provenance_sha256,
            "record_count": self.record_count,
            "split": self.split.value,
        }

    @classmethod
    def from_payload(cls, payload: object) -> LearningShard:
        if not isinstance(payload, dict) or frozenset(payload) != _SHARD_KEYS:
            raise LearningPackageIntegrityError("learning shard keys are invalid")
        split_value = payload.get("split")
        try:
            split = LearningDataSplit(split_value)
        except (TypeError, ValueError) as exc:
            raise LearningPackageIntegrityError("learning shard split is invalid") from exc
        try:
            return cls(
                split=split,
                artifact_sha256=payload.get("artifact_sha256"),
                provenance_sha256=payload.get("provenance_sha256"),
                license_evidence_sha256=payload.get("license_evidence_sha256"),
                record_count=payload.get("record_count"),
                byte_count=payload.get("byte_count"),
            )
        except LearningPackageValidationError as exc:
            raise LearningPackageIntegrityError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class FrozenLearningPackage:
    package_id: str
    package_version: str
    base_artifact_sha256: str
    selection_policy_sha256: str
    verification_sha256: str
    evaluation_set_sha256: str
    shards: tuple[LearningShard, ...]
    schema_version: int = _SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != _SCHEMA_VERSION:
            raise LearningPackageValidationError("unsupported learning-package schema")
        _require_token(self.package_id, field="package_id")
        _require_token(self.package_version, field="package_version")
        _require_sha256(self.base_artifact_sha256, field="base_artifact_sha256")
        _require_sha256(self.selection_policy_sha256, field="selection_policy_sha256")
        _require_sha256(self.verification_sha256, field="verification_sha256")
        _require_sha256(self.evaluation_set_sha256, field="evaluation_set_sha256")
        if type(self.shards) is not tuple:
            raise LearningPackageValidationError("shards must be an immutable tuple")
        if not 1 <= len(self.shards) <= _MAX_SHARDS:
            raise LearningPackageValidationError("shard count is outside the supported bound")
        if not all(type(shard) is LearningShard for shard in self.shards):
            raise LearningPackageValidationError("shards must contain exact LearningShard values")
        if self.shards != _canonical_shard_order(self.shards):
            raise LearningPackageValidationError("learning-package shard order is not canonical")

        identities = [shard.artifact_sha256 for shard in self.shards]
        if len(set(identities)) != len(identities):
            raise LearningPackageValidationError("artifact SHA-256 values must be unique")
        if self.evaluation_set_sha256 in identities:
            raise LearningPackageValidationError(
                "held-out evaluation artifact must be separate from training package shards"
            )

        training_count = sum(shard.split is LearningDataSplit.TRAINING for shard in self.shards)
        validation_count = sum(shard.split is LearningDataSplit.VALIDATION for shard in self.shards)
        if training_count == 0:
            raise LearningPackageValidationError("at least one training shard is required")
        if validation_count == 0:
            raise LearningPackageValidationError("at least one validation shard is required")

        total_records = sum(shard.record_count for shard in self.shards)
        total_bytes = sum(shard.byte_count for shard in self.shards)
        if total_records > _MAX_SIGNED_64 or total_bytes > _MAX_SIGNED_64:
            raise LearningPackageValidationError("aggregate shard size exceeds signed-64 bounds")

    @classmethod
    def freeze(
        cls,
        *,
        package_id: str,
        package_version: str,
        base_artifact_sha256: str,
        selection_policy_sha256: str,
        verification_sha256: str,
        evaluation_set_sha256: str,
        shards: Iterable[LearningShard],
    ) -> FrozenLearningPackage:
        shard_values = tuple(shards)
        if not all(type(shard) is LearningShard for shard in shard_values):
            raise LearningPackageValidationError("shards must contain exact LearningShard values")
        return cls(
            package_id=package_id,
            package_version=package_version,
            base_artifact_sha256=base_artifact_sha256,
            selection_policy_sha256=selection_policy_sha256,
            verification_sha256=verification_sha256,
            evaluation_set_sha256=evaluation_set_sha256,
            shards=_canonical_shard_order(shard_values),
        )

    def candidate_dataset_payload(self) -> dict[str, object]:
        return {
            "selection_policy_sha256": self.selection_policy_sha256,
            "shards": [shard.canonical_payload() for shard in self.shards],
            "verification_sha256": self.verification_sha256,
        }

    @property
    def candidate_dataset_sha256(self) -> str:
        return _digest_payload(self.candidate_dataset_payload())

    def canonical_payload(self) -> dict[str, object]:
        return {
            "base_artifact_sha256": self.base_artifact_sha256,
            "candidate_dataset_sha256": self.candidate_dataset_sha256,
            "evaluation_set_sha256": self.evaluation_set_sha256,
            "package_id": self.package_id,
            "package_version": self.package_version,
            "schema_version": self.schema_version,
            "selection_policy_sha256": self.selection_policy_sha256,
            "shards": [shard.canonical_payload() for shard in self.shards],
            "verification_sha256": self.verification_sha256,
        }

    @property
    def manifest_sha256(self) -> str:
        return _digest_payload(self.canonical_payload())

    def to_json(self) -> str:
        payload = self.canonical_payload()
        envelope = {
            "manifest": payload,
            "manifest_sha256": _digest_payload(payload),
        }
        return _canonical_json_bytes(envelope).decode("utf-8")

    @classmethod
    def from_json(
        cls,
        raw: str | bytes,
        *,
        expected_manifest_sha256: str | None = None,
    ) -> FrozenLearningPackage:
        if isinstance(raw, str):
            if not raw or len(raw) > _MAX_MANIFEST_BYTES:
                raise LearningPackageIntegrityError("serialized package size is invalid")
            decoded = raw
            try:
                encoded = raw.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise LearningPackageIntegrityError("serialized package is not valid UTF-8") from exc
            if len(encoded) > _MAX_MANIFEST_BYTES:
                raise LearningPackageIntegrityError("serialized package size is invalid")
        elif isinstance(raw, bytes):
            if not raw or len(raw) > _MAX_MANIFEST_BYTES:
                raise LearningPackageIntegrityError("serialized package size is invalid")
            encoded = raw
            try:
                decoded = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise LearningPackageIntegrityError("serialized package is not valid UTF-8") from exc
        else:
            raise LearningPackageIntegrityError("serialized package must be str or bytes")
        try:
            parsed = json.loads(decoded, object_pairs_hook=_reject_duplicate_keys)
        except json.JSONDecodeError as exc:
            raise LearningPackageIntegrityError("serialized package is not valid JSON") from exc
        if not isinstance(parsed, dict) or frozenset(parsed) != _ENVELOPE_KEYS:
            raise LearningPackageIntegrityError("learning-package envelope keys are invalid")

        if expected_manifest_sha256 is not None:
            try:
                trusted_digest = _require_sha256(
                    expected_manifest_sha256,
                    field="expected_manifest_sha256",
                )
            except LearningPackageValidationError as exc:
                raise LearningPackageIntegrityError(str(exc)) from exc
            if parsed.get("manifest_sha256") != trusted_digest:
                raise LearningPackageIntegrityError("trusted learning-package digest mismatch")

        manifest = parsed.get("manifest")
        declared_digest = parsed.get("manifest_sha256")
        if not isinstance(manifest, dict) or frozenset(manifest) != _MANIFEST_KEYS:
            raise LearningPackageIntegrityError("learning-package manifest keys are invalid")
        if not isinstance(declared_digest, str) or not _SHA256_RE.fullmatch(declared_digest):
            raise LearningPackageIntegrityError("learning-package digest format is invalid")
        if _digest_payload(manifest) != declared_digest:
            raise LearningPackageIntegrityError("learning-package digest mismatch")

        shards_value = manifest.get("shards")
        if not isinstance(shards_value, list):
            raise LearningPackageIntegrityError("learning-package shards must be a list")
        try:
            package = cls(
                schema_version=manifest.get("schema_version"),
                package_id=manifest.get("package_id"),
                package_version=manifest.get("package_version"),
                base_artifact_sha256=manifest.get("base_artifact_sha256"),
                selection_policy_sha256=manifest.get("selection_policy_sha256"),
                verification_sha256=manifest.get("verification_sha256"),
                evaluation_set_sha256=manifest.get("evaluation_set_sha256"),
                shards=tuple(LearningShard.from_payload(item) for item in shards_value),
            )
        except LearningPackageValidationError as exc:
            raise LearningPackageIntegrityError(str(exc)) from exc

        if manifest.get("candidate_dataset_sha256") != package.candidate_dataset_sha256:
            raise LearningPackageIntegrityError("candidate dataset digest mismatch")
        if decoded != package.to_json():
            raise LearningPackageIntegrityError("learning-package serialization is not canonical")
        return package
