from __future__ import annotations

import hashlib
import json

import pytest

import nika_core.training_scale as scale
from nika_core.data.sqlite import SQLiteStore
from nika_core.training_scale_progression_repository import (
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
