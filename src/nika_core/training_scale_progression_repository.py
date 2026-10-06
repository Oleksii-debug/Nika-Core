from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime

from nika_core.data.sqlite import SQLiteStore
from nika_core.training_scale import (
    TrainingScaleError,
    TrainingScaleProgressionProof,
    _build_progression_proof,
    _validated_progression_payload,
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_PROOF_JSON_BYTES = 64 * 1024


class TrainingScaleProgressionRepositoryError(RuntimeError):
    """Durable scale progression authority is absent, conflicting, or corrupt."""


def _reject_duplicate_fields(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate progression authority JSON field")
        result[key] = value
    return result


def _reject_json_constant(_: str) -> object:
    raise ValueError("non-finite progression authority JSON constant")


def _reject_json_float(_: str) -> float:
    raise ValueError("progression authority JSON floats are forbidden")


def _encode_payload(payload: dict[str, object]) -> str:
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise TrainingScaleProgressionRepositoryError(
            "progression authority payload is not canonical JSON"
        ) from exc
    if len(encoded.encode("utf-8")) > _MAX_PROOF_JSON_BYTES:
        raise TrainingScaleProgressionRepositoryError(
            "progression authority payload exceeds the configured byte limit"
        )
    return encoded


def _decode_payload(raw: object) -> dict[str, object]:
    if type(raw) is not str:
        raise TrainingScaleProgressionRepositoryError(
            "stored progression authority must be UTF-8 JSON text"
        )
    if len(raw.encode("utf-8")) > _MAX_PROOF_JSON_BYTES:
        raise TrainingScaleProgressionRepositoryError(
            "stored progression authority exceeds the configured byte limit"
        )
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_fields,
            parse_constant=_reject_json_constant,
            parse_float=_reject_json_float,
        )
        return _validated_progression_payload(value)
    except (TrainingScaleError, TypeError, ValueError) as exc:
        raise TrainingScaleProgressionRepositoryError(
            "stored progression authority is not canonical"
        ) from exc


def _restore_stored_proof(
    payload: dict[str, object],
    *,
    expected_proof_sha256: str,
) -> TrainingScaleProgressionProof:
    try:
        proof = _build_progression_proof(
            plan_sha256=payload["plan_sha256"],
            tier_index=payload["tier_index"],
            authorization_sha256=payload["authorization_sha256"],
            job_id=payload["job_id"],
            job_fingerprint=payload["job_fingerprint"],
            base_artifact_ref=payload["base_artifact_ref"],
            base_sha256=payload["base_sha256"],
            candidate_artifact_ref=payload["candidate_artifact_ref"],
            candidate_sha256=payload["candidate_sha256"],
            frozen_package_sha256=payload["frozen_package_sha256"],
            training_material_sha256=payload["training_material_sha256"],
            execution_plan_sha256=payload["execution_plan_sha256"],
            comparison_evidence_sha256=payload["comparison_evidence_sha256"],
            evaluation_set_sha256=payload["evaluation_set_sha256"],
        )
    except (TrainingScaleError, TypeError, ValueError) as exc:
        raise TrainingScaleProgressionRepositoryError(
            "stored progression authority could not be restored"
        ) from exc
    if proof.proof_sha256 != expected_proof_sha256:
        raise TrainingScaleProgressionRepositoryError(
            "stored progression authority digest does not match its key"
        )
    return proof


class SQLiteTrainingScaleProgressionRepository:
    """Internal durable authority for already-built scale progression proofs.

    The repository never upgrades caller JSON into authority. put() accepts only
    an exact revalidated proof produced by the trusted build path. Restore either
    uses a previously bound digest or requires byte-for-byte canonical equality
    with a claim already present in this product-managed SQLite database.
    """

    def __init__(self, store: SQLiteStore) -> None:
        if type(store) is not SQLiteStore:
            raise TypeError("store must be an exact SQLiteStore")
        self._store = store

    def put(self, proof: TrainingScaleProgressionProof) -> TrainingScaleProgressionProof:
        if type(proof) is not TrainingScaleProgressionProof:
            raise TypeError("proof must be an exact TrainingScaleProgressionProof")
        canonical = proof.revalidated()
        payload = canonical.canonical_payload()
        encoded = _encode_payload(payload)
        proof_sha256 = canonical.proof_sha256
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT proof_json FROM training_scale_progression_authority "
                "WHERE proof_sha256 = ?",
                (proof_sha256,),
            ).fetchone()
            if row is not None:
                if row["proof_json"] != encoded:
                    raise TrainingScaleProgressionRepositoryError(
                        "progression authority digest collides with different stored evidence"
                    )
                return canonical
            try:
                conn.execute(
                    "INSERT INTO training_scale_progression_authority("
                    "proof_sha256, proof_json, plan_sha256, tier_index, "
                    "authorization_sha256, job_id, candidate_artifact_ref, "
                    "candidate_sha256, comparison_evidence_sha256, "
                    "evaluation_set_sha256, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        proof_sha256,
                        encoded,
                        canonical.plan_sha256,
                        canonical.tier_index,
                        canonical.authorization_sha256,
                        canonical.job_id,
                        canonical.candidate_artifact_ref,
                        canonical.candidate_sha256,
                        canonical.comparison_evidence_sha256,
                        canonical.evaluation_set_sha256,
                        datetime.now(UTC).isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise TrainingScaleProgressionRepositoryError(
                    "progression authority conflicts with existing durable evidence"
                ) from exc
        return canonical

    def get(self, proof_sha256: str) -> TrainingScaleProgressionProof:
        if type(proof_sha256) is not str or _SHA256_RE.fullmatch(proof_sha256) is None:
            raise TrainingScaleProgressionRepositoryError(
                "progression authority key must be an exact lowercase SHA-256 digest"
            )
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT proof_sha256, proof_json "
                "FROM training_scale_progression_authority WHERE proof_sha256 = ?",
                (proof_sha256,),
            ).fetchone()
        if row is None:
            raise TrainingScaleProgressionRepositoryError(
                "trusted progression authority was not found"
            )
        payload = _decode_payload(row["proof_json"])
        return _restore_stored_proof(
            payload,
            expected_proof_sha256=row["proof_sha256"],
        )

    def get_matching_claim(self, value: object) -> TrainingScaleProgressionProof:
        try:
            claim = _validated_progression_payload(value)
        except TrainingScaleError as exc:
            raise TrainingScaleProgressionRepositoryError(
                "progression authority claim is not canonical"
            ) from exc
        encoded = _encode_payload(claim)
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT proof_sha256, proof_json "
                "FROM training_scale_progression_authority WHERE proof_json = ?",
                (encoded,),
            ).fetchone()
        if row is None:
            raise TrainingScaleProgressionRepositoryError(
                "trusted progression authority matching the claim was not found"
            )
        payload = _decode_payload(row["proof_json"])
        proof = _restore_stored_proof(
            payload,
            expected_proof_sha256=row["proof_sha256"],
        )
        if proof.canonical_payload() != claim:
            raise TrainingScaleProgressionRepositoryError(
                "stored progression authority does not match the requested claim"
            )
        return proof
