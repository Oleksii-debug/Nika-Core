from __future__ import annotations

import hashlib
import json

import pytest

from nika_core.learning_package import (
    FrozenLearningPackage,
    LearningDataSplit,
    LearningPackageIntegrityError,
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
K = "4" * 64


class _EncodeBomb(str):
    def encode(self, encoding: str = "utf-8", errors: str = "strict") -> bytes:
        raise AssertionError("oversized str must be rejected before encode")


class _DecodeBomb(bytes):
    def decode(self, encoding: str = "utf-8", errors: str = "strict") -> str:
        raise AssertionError("oversized bytes must be rejected before decode")


class _PayloadSpoofingShard(LearningShard):
    def canonical_payload(self) -> dict[str, object]:
        payload = super().canonical_payload()
        payload["artifact_sha256"] = F
        payload["record_count"] = self.record_count + 1
        return payload


def _shard(
    split: LearningDataSplit,
    artifact: str,
    provenance: str,
    license_evidence: str,
    *,
    records: int = 10,
    bytes_: int = 100,
) -> LearningShard:
    return LearningShard(
        split=split,
        artifact_sha256=artifact,
        provenance_sha256=provenance,
        license_evidence_sha256=license_evidence,
        record_count=records,
        byte_count=bytes_,
    )


def _package(*, reverse: bool = False) -> FrozenLearningPackage:
    shards = [
        _shard(LearningDataSplit.TRAINING, A, C, E),
        _shard(LearningDataSplit.VALIDATION, B, D, F),
    ]
    if reverse:
        shards.reverse()
    return FrozenLearningPackage.freeze(
        package_id="nika-12-6-adapt",
        package_version="v1",
        base_artifact_sha256=G,
        selection_policy_sha256=H,
        verification_sha256=I,
        evaluation_set_sha256=J,
        shards=shards,
    )


def test_freeze_is_order_independent_and_binds_candidate_dataset() -> None:
    first = _package()
    second = _package(reverse=True)

    assert first.shards == second.shards
    assert first.candidate_dataset_sha256 == second.candidate_dataset_sha256
    assert first.manifest_sha256 == second.manifest_sha256
    assert first.to_json() == second.to_json()


def test_round_trip_revalidates_exact_manifest() -> None:
    package = _package()

    restored = FrozenLearningPackage.from_json(package.to_json())

    assert restored == package
    assert restored.manifest_sha256 == package.manifest_sha256


def test_serialized_manifest_rejects_digest_preserving_field_tamper() -> None:
    package = _package()
    envelope = json.loads(package.to_json())
    envelope["manifest"]["base_artifact_sha256"] = K

    with pytest.raises(LearningPackageIntegrityError, match="digest mismatch"):
        FrozenLearningPackage.from_json(json.dumps(envelope))


def test_serialized_manifest_rejects_recomputed_outer_digest_if_dataset_digest_stale() -> None:
    package = _package()
    envelope = json.loads(package.to_json())
    envelope["manifest"]["shards"][0]["record_count"] = 11
    canonical = json.dumps(
        envelope["manifest"],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    envelope["manifest_sha256"] = hashlib.sha256(canonical).hexdigest()

    with pytest.raises(LearningPackageIntegrityError, match="candidate dataset digest mismatch"):
        FrozenLearningPackage.from_json(json.dumps(envelope))


def test_held_out_split_cannot_be_smuggled_into_training_package() -> None:
    package = _package()
    envelope = json.loads(package.to_json())
    envelope["manifest"]["shards"][0]["split"] = "held_out"
    manifest = envelope["manifest"]
    canonical = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    envelope["manifest_sha256"] = hashlib.sha256(canonical).hexdigest()

    with pytest.raises(LearningPackageIntegrityError, match="split is invalid"):
        FrozenLearningPackage.from_json(json.dumps(envelope))


def test_held_out_artifact_digest_must_not_equal_training_or_validation_artifact() -> None:
    training = _shard(LearningDataSplit.TRAINING, A, C, E)
    validation = _shard(LearningDataSplit.VALIDATION, B, D, F)

    with pytest.raises(LearningPackageValidationError, match="held-out evaluation artifact"):
        FrozenLearningPackage.freeze(
            package_id="candidate",
            package_version="v1",
            base_artifact_sha256=G,
            selection_policy_sha256=H,
            verification_sha256=I,
            evaluation_set_sha256=A,
            shards=(training, validation),
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("artifact_sha256", "A" * 64),
        ("artifact_sha256", "a" * 63),
        ("record_count", True),
        ("record_count", 0),
        ("byte_count", -1),
    ],
)
def test_shard_rejects_noncanonical_identity_and_counts(field: str, value: object) -> None:
    values: dict[str, object] = {
        "split": LearningDataSplit.TRAINING,
        "artifact_sha256": A,
        "provenance_sha256": C,
        "license_evidence_sha256": E,
        "record_count": 1,
        "byte_count": 1,
    }
    values[field] = value

    with pytest.raises(LearningPackageValidationError):
        LearningShard(**values)


def test_package_requires_training_and_validation_and_unique_artifacts() -> None:
    training = _shard(LearningDataSplit.TRAINING, A, C, E)

    with pytest.raises(LearningPackageValidationError, match="validation shard"):
        FrozenLearningPackage.freeze(
            package_id="candidate",
            package_version="v1",
            base_artifact_sha256=G,
            selection_policy_sha256=H,
            verification_sha256=I,
            evaluation_set_sha256=J,
            shards=(training,),
        )

    with pytest.raises(LearningPackageValidationError, match="unique"):
        FrozenLearningPackage.freeze(
            package_id="candidate",
            package_version="v1",
            base_artifact_sha256=G,
            selection_policy_sha256=H,
            verification_sha256=I,
            evaluation_set_sha256=J,
            shards=(
                training,
                _shard(LearningDataSplit.VALIDATION, A, D, F),
            ),
        )


def test_package_rejects_path_url_and_control_text_identifiers() -> None:
    for package_id in ("C:/Users/name/data", "https://example.test/data", "bad\nname"):
        with pytest.raises(LearningPackageValidationError, match="package_id"):
            FrozenLearningPackage.freeze(
                package_id=package_id,
                package_version="v1",
                base_artifact_sha256=G,
                selection_policy_sha256=H,
                verification_sha256=I,
                evaluation_set_sha256=J,
                shards=(
                    _shard(LearningDataSplit.TRAINING, A, C, E),
                    _shard(LearningDataSplit.VALIDATION, B, D, F),
                ),
            )


def test_trusted_manifest_digest_blocks_fully_recomputed_substitution() -> None:
    package = _package()
    envelope = json.loads(package.to_json())
    original_digest = package.manifest_sha256
    envelope["manifest"]["package_version"] = "v2"
    manifest = envelope["manifest"]
    canonical_dataset = json.dumps(
        {
            "selection_policy_sha256": manifest["selection_policy_sha256"],
            "shards": manifest["shards"],
            "verification_sha256": manifest["verification_sha256"],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    manifest["candidate_dataset_sha256"] = hashlib.sha256(canonical_dataset).hexdigest()
    canonical_manifest = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    envelope["manifest_sha256"] = hashlib.sha256(canonical_manifest).hexdigest()

    with pytest.raises(LearningPackageIntegrityError, match="trusted learning-package digest"):
        FrozenLearningPackage.from_json(
            json.dumps(envelope),
            expected_manifest_sha256=original_digest,
        )


def test_serialized_manifest_rejects_duplicate_keys_and_extra_keys() -> None:
    package = _package()
    raw = package.to_json()
    duplicate = raw.replace(
        '"package_id":"nika-12-6-adapt"',
        '"package_id":"x","package_id":"nika-12-6-adapt"',
    )

    with pytest.raises(LearningPackageIntegrityError, match="duplicate JSON key"):
        FrozenLearningPackage.from_json(duplicate)

    envelope = json.loads(raw)
    envelope["manifest"]["unexpected"] = "value"
    with pytest.raises(LearningPackageIntegrityError, match="manifest keys are invalid"):
        FrozenLearningPackage.from_json(json.dumps(envelope))


def test_aggregate_counts_fail_closed_before_signed_64_overflow() -> None:
    max_value = (1 << 63) - 1
    training = _shard(
        LearningDataSplit.TRAINING,
        A,
        C,
        E,
        records=max_value,
        bytes_=max_value,
    )
    validation = _shard(
        LearningDataSplit.VALIDATION,
        B,
        D,
        F,
        records=1,
        bytes_=1,
    )

    with pytest.raises(LearningPackageValidationError, match="aggregate shard size"):
        FrozenLearningPackage.freeze(
            package_id="candidate",
            package_version="v1",
            base_artifact_sha256=G,
            selection_policy_sha256=H,
            verification_sha256=I,
            evaluation_set_sha256=J,
            shards=(training, validation),
        )


def test_direct_constructor_rejects_noncanonical_shard_order() -> None:
    training = _shard(LearningDataSplit.TRAINING, A, C, E)
    validation = _shard(LearningDataSplit.VALIDATION, B, D, F)

    with pytest.raises(LearningPackageValidationError, match="shard order is not canonical"):
        FrozenLearningPackage(
            package_id="candidate",
            package_version="v1",
            base_artifact_sha256=G,
            selection_policy_sha256=H,
            verification_sha256=I,
            evaluation_set_sha256=J,
            shards=(validation, training),
        )


def test_package_rejects_learning_shard_subclasses_before_digest_trust() -> None:
    spoofed_training = _PayloadSpoofingShard(
        split=LearningDataSplit.TRAINING,
        artifact_sha256=A,
        provenance_sha256=C,
        license_evidence_sha256=E,
        record_count=10,
        byte_count=100,
    )
    validation = _shard(LearningDataSplit.VALIDATION, B, D, K)

    with pytest.raises(LearningPackageValidationError, match="exact LearningShard"):
        FrozenLearningPackage.freeze(
            package_id="candidate",
            package_version="v1",
            base_artifact_sha256=G,
            selection_policy_sha256=H,
            verification_sha256=I,
            evaluation_set_sha256=J,
            shards=(spoofed_training, validation),
        )

    with pytest.raises(LearningPackageValidationError, match="exact LearningShard"):
        FrozenLearningPackage(
            package_id="candidate",
            package_version="v1",
            base_artifact_sha256=G,
            selection_policy_sha256=H,
            verification_sha256=I,
            evaluation_set_sha256=J,
            shards=(spoofed_training, validation),
        )


def test_serialized_manifest_rejects_non_utf8_byte_encoding() -> None:
    package = _package()

    with pytest.raises(LearningPackageIntegrityError, match="not valid UTF-8"):
        FrozenLearningPackage.from_json(package.to_json().encode("utf-16"))


def test_serialized_manifest_rejects_noncanonical_json_representation() -> None:
    package = _package()
    envelope = json.loads(package.to_json())
    noncanonical = json.dumps(envelope, indent=2)

    with pytest.raises(LearningPackageIntegrityError, match="serialization is not canonical"):
        FrozenLearningPackage.from_json(noncanonical)


def test_oversized_string_rejects_before_utf8_encode() -> None:
    oversized = _EncodeBomb("x" * (1024 * 1024 + 1))

    with pytest.raises(LearningPackageIntegrityError, match="size is invalid"):
        FrozenLearningPackage.from_json(oversized)


def test_oversized_bytes_reject_before_utf8_decode() -> None:
    oversized = _DecodeBomb(b"x" * (1024 * 1024 + 1))

    with pytest.raises(LearningPackageIntegrityError, match="size is invalid"):
        FrozenLearningPackage.from_json(oversized)


def test_multibyte_string_enforces_post_encode_byte_bound() -> None:
    oversized_utf8 = "€" * 400_000

    with pytest.raises(LearningPackageIntegrityError, match="size is invalid"):
        FrozenLearningPackage.from_json(oversized_utf8)
