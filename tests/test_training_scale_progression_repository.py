from __future__ import annotations

import hashlib
import json

import pytest

import nika_core.training_scale as scale
from nika_core.data.sqlite import SQLiteStore
from nika_core.learning_package import FrozenLearningPackage, LearningDataSplit, LearningShard
from nika_core.training_materials import (
    TrainingMaterialEvidence,
    TrainingMaterialSetEvidence,
)
from nika_core.training_runtime import ArtifactIdentity
from nika_core.training_scale_progression_repository import (
    SQLiteTrainingScaleAuthorizationRepository,
    SQLiteTrainingScaleProgressionRepository,
    TrainingScaleProgressionRepositoryError,
)
from nika_core.training_scale_progression_schema import (
    TRAINING_SCALE_PROGRESSION_SCHEMA_VERSION,
)


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _proof() -> scale.TrainingScaleProgressionProof:
    return scale._build_progression_proof(
        plan_sha256=_sha(b"plan"),
        tier_index=0,
        authorization_sha256=_sha(b"authorization"),
        job_id="pilot-job",
        job_fingerprint=_sha(b"job"),
        base_artifact_ref="models/base",
        base_sha256=_sha(b"base"),
        candidate_artifact_ref="models/candidate",
        candidate_sha256=_sha(b"candidate"),
        frozen_package_sha256=_sha(b"package"),
        training_material_sha256=_sha(b"materials"),
        execution_plan_sha256=_sha(b"execution"),
        comparison_evidence_sha256=_sha(b"comparison"),
        evaluation_set_sha256=_sha(b"evaluation"),
    )


def test_progression_repository_round_trip_and_idempotent_put(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    repository = SQLiteTrainingScaleProgressionRepository(store)
    proof = _proof()

    first = repository.put(proof)
    second = repository.put(proof)
    restored = repository.get(proof.proof_sha256)

    assert first.proof_sha256 == proof.proof_sha256
    assert second.proof_sha256 == proof.proof_sha256
    assert restored.canonical_payload() == proof.canonical_payload()


def test_progression_repository_claim_requires_preexisting_authority(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    repository = SQLiteTrainingScaleProgressionRepository(store)
    proof = _proof()

    with pytest.raises(
        TrainingScaleProgressionRepositoryError,
        match="matching the claim was not found",
    ):
        repository.get_matching_claim(proof.canonical_payload())

    repository.put(proof)

    restored = repository.get_matching_claim(proof.canonical_payload())
    assert restored.proof_sha256 == proof.proof_sha256


def test_progression_repository_rejects_claim_different_from_stored_authority(
    tmp_path,
) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    repository = SQLiteTrainingScaleProgressionRepository(store)
    proof = _proof()
    repository.put(proof)
    claim = proof.canonical_payload()
    claim["comparison_evidence_sha256"] = _sha(b"forged-comparison")

    with pytest.raises(
        TrainingScaleProgressionRepositoryError,
        match="matching the claim was not found",
    ):
        repository.get_matching_claim(claim)


def test_progression_repository_detects_stored_digest_corruption(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    repository = SQLiteTrainingScaleProgressionRepository(store)
    proof = _proof()
    repository.put(proof)

    with store.connection() as conn:
        conn.execute(
            "UPDATE training_scale_progression_authority "
            "SET proof_sha256 = ? WHERE proof_sha256 = ?",
            (_sha(b"wrong-key"), proof.proof_sha256),
        )

    with pytest.raises(
        TrainingScaleProgressionRepositoryError,
        match="digest does not match",
    ):
        repository.get(_sha(b"wrong-key"))


def test_progression_repository_rejects_noncanonical_stored_json(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    repository = SQLiteTrainingScaleProgressionRepository(store)
    proof = _proof()
    repository.put(proof)

    raw = json.dumps(proof.canonical_payload(), sort_keys=True, separators=(",", ":"))
    corrupted = raw[:-1] + ',"tier_index":0}'
    with store.connection() as conn:
        conn.execute(
            "UPDATE training_scale_progression_authority "
            "SET proof_json = ? WHERE proof_sha256 = ?",
            (corrupted, proof.proof_sha256),
        )

    with pytest.raises(
        TrainingScaleProgressionRepositoryError,
        match="not canonical",
    ):
        repository.get(proof.proof_sha256)


def test_progression_schema_migration_is_applied_once(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    store.initialize()

    with store.connection() as conn:
        row = conn.execute(
            "SELECT MAX(version) AS version "
            "FROM training_scale_progression_schema_migrations"
        ).fetchone()
        table = conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'training_scale_progression_authority'"
        ).fetchone()

    assert int(row["version"]) == TRAINING_SCALE_PROGRESSION_SCHEMA_VERSION
    assert table is not None

def _authorization(
    *,
    progression_proof: scale.TrainingScaleProgressionProof | None = None,
) -> scale.TrainingScaleAuthorization:
    base_sha256 = (
        _sha(b"base")
        if progression_proof is None
        else progression_proof.candidate_sha256
    )
    shards = (
        LearningShard(
            split=LearningDataSplit.TRAINING,
            artifact_sha256=_sha(b"training"),
            provenance_sha256=_sha(b"training-provenance"),
            license_evidence_sha256=_sha(b"training-license"),
            record_count=1,
            byte_count=8,
        ),
        LearningShard(
            split=LearningDataSplit.VALIDATION,
            artifact_sha256=_sha(b"validation"),
            provenance_sha256=_sha(b"validation-provenance"),
            license_evidence_sha256=_sha(b"validation-license"),
            record_count=1,
            byte_count=10,
        ),
    )
    package = FrozenLearningPackage.freeze(
        package_id="authority-package",
        package_version="1",
        base_artifact_sha256=base_sha256,
        selection_policy_sha256=_sha(b"selection"),
        verification_sha256=_sha(b"verification"),
        evaluation_set_sha256=_sha(b"evaluation"),
        shards=shards,
    )
    evidence = TrainingMaterialSetEvidence.from_package(
        package,
        workspace_sha256=_sha(b"workspace"),
        materials=tuple(TrainingMaterialEvidence.from_shard(item) for item in shards),
    )
    plan = scale.TrainingScalePlan(
        plan_id="durable-scale",
        evaluation_set_sha256=evidence.evaluation_set_sha256,
        tiers=(
            scale.TrainingScaleTier(
                tier_id="pilot",
                max_training_records=1,
                max_training_bytes=8,
                max_validation_records=1,
                max_validation_bytes=10,
                max_steps=2,
            ),
            scale.TrainingScaleTier(
                tier_id="small",
                max_training_records=2,
                max_training_bytes=16,
                max_validation_records=2,
                max_validation_bytes=20,
                max_steps=4,
            ),
        ),
    )
    tier_id = "pilot" if progression_proof is None else "small"
    base_ref = (
        "models/base"
        if progression_proof is None
        else progression_proof.candidate_artifact_ref
    )
    return scale.authorize_training_scale(
        plan=plan,
        tier_id=tier_id,
        job_id="pilot-job" if progression_proof is None else "small-job",
        base_artifact=ArtifactIdentity(base_ref, base_sha256),
        candidate_artifact_ref=(
            "models/candidate" if progression_proof is None else "models/small-candidate"
        ),
        material_evidence=evidence,
        execution_plan_sha256=_sha(b"execution"),
        max_steps=2 if progression_proof is None else 4,
        progression_proof=progression_proof,
    )


def test_authorization_repository_round_trip_pilot_authority(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    repository = SQLiteTrainingScaleAuthorizationRepository(store)
    authorization = _authorization()

    repository.put(authorization)
    restored = repository.get(authorization.authorization_sha256)

    assert restored.canonical_payload() == authorization.canonical_payload()
    assert restored.authorization_sha256 == authorization.authorization_sha256


def test_authorization_repository_carries_trusted_previous_proof(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    progression = _proof()
    authorization = _authorization(progression_proof=progression)
    repository = SQLiteTrainingScaleAuthorizationRepository(store)

    repository.put(authorization)
    restored = repository.get(authorization.authorization_sha256)

    assert restored.authorization_sha256 == authorization.authorization_sha256
    assert restored.progression_proof is not None
    assert restored.progression_proof.proof_sha256 == progression.proof_sha256


def test_authorization_repository_rejects_missing_previous_proof_row(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    progression = _proof()
    authorization = _authorization(progression_proof=progression)
    repository = SQLiteTrainingScaleAuthorizationRepository(store)
    repository.put(authorization)

    with store.connection() as conn:
        conn.execute(
            "DELETE FROM training_scale_progression_authority "
            "WHERE proof_sha256 = ?",
            (progression.proof_sha256,),
        )

    with pytest.raises(
        TrainingScaleProgressionRepositoryError,
        match="trusted progression authority was not found",
    ):
        repository.get(authorization.authorization_sha256)


def test_authorization_repository_detects_digest_corruption(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "state.sqlite3")
    store.initialize()
    authorization = _authorization()
    repository = SQLiteTrainingScaleAuthorizationRepository(store)
    repository.put(authorization)
    wrong = _sha(b"wrong-authorization-key")

    with store.connection() as conn:
        conn.execute(
            "UPDATE training_scale_authorization_authority "
            "SET authorization_sha256 = ? WHERE authorization_sha256 = ?",
            (wrong, authorization.authorization_sha256),
        )

    with pytest.raises(
        TrainingScaleProgressionRepositoryError,
        match="digest does not match",
    ):
        repository.get(wrong)

