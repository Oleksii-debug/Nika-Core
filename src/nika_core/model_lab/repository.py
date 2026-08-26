from __future__ import annotations

import json
from datetime import UTC, datetime
from hashlib import sha256
from typing import Protocol

from nika_core.data.sqlite import SQLiteStore
from nika_core.model_gateway.contracts import ProviderKind
from nika_core.model_lab.benchmark import evidence_document, evidence_sha256
from nika_core.model_lab.contracts import (
    AttemptStatus,
    BenchmarkAttempt,
    BenchmarkRunEvidence,
    MetricValue,
    ModelCandidate,
)
from nika_core.model_lab.experiment_adapter import candidate_identity_sha256
from nika_core.resources.contracts import ResourceSnapshot


_MODEL_LAB_SCHEMA_VERSION = 1
_MODEL_LAB_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        "CREATE TABLE model_lab_candidates ("
        "candidate_id TEXT PRIMARY KEY, "
        "candidate_json TEXT NOT NULL, "
        "identity_sha256 TEXT NOT NULL, "
        "created_at TEXT NOT NULL"
        ")",
        "CREATE TABLE model_lab_runs ("
        "run_id TEXT PRIMARY KEY, "
        "candidate_id TEXT NOT NULL, "
        "candidate_identity_sha256 TEXT NOT NULL, "
        "suite_id TEXT NOT NULL, "
        "suite_version TEXT NOT NULL, "
        "suite_sha256 TEXT NOT NULL, "
        "evidence_json TEXT NOT NULL, "
        "evidence_sha256 TEXT NOT NULL, "
        "created_at TEXT NOT NULL, "
        "FOREIGN KEY(candidate_id) REFERENCES model_lab_candidates(candidate_id)"
        ")",
        "CREATE INDEX model_lab_runs_candidate_idx "
        "ON model_lab_runs(candidate_id, created_at)",
        "CREATE INDEX model_lab_runs_suite_idx "
        "ON model_lab_runs(suite_id, suite_version, created_at)",
    ),
}


class ModelLabRepository(Protocol):
    def register_candidate(self, candidate: ModelCandidate) -> None: ...

    def get_candidate(self, candidate_id: str) -> ModelCandidate: ...

    def list_candidates(self) -> tuple[ModelCandidate, ...]: ...

    def record_run(self, evidence: BenchmarkRunEvidence) -> None: ...

    def get_run(self, run_id: str) -> BenchmarkRunEvidence: ...


class SQLiteModelLabRepository:
    """Durable F6 registry with immutable, idempotent benchmark evidence."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def initialize(self) -> None:
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS model_lab_schema_migrations ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            row = conn.execute(
                "SELECT MAX(version) AS version FROM model_lab_schema_migrations"
            ).fetchone()
            current = int(row["version"] or 0)
            if current > _MODEL_LAB_SCHEMA_VERSION:
                raise RuntimeError(
                    f"model lab schema {current} is newer than supported "
                    f"schema {_MODEL_LAB_SCHEMA_VERSION}"
                )
            for version in range(current + 1, _MODEL_LAB_SCHEMA_VERSION + 1):
                statements = _MODEL_LAB_MIGRATIONS.get(version)
                if statements is None:
                    raise RuntimeError(f"missing model lab migration {version}")
                for statement in statements:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO model_lab_schema_migrations(version, applied_at) "
                    "VALUES (?, ?)",
                    (version, datetime.now(UTC).isoformat()),
                )

    def register_candidate(self, candidate: ModelCandidate) -> None:
        payload = _candidate_document(candidate)
        identity = candidate_identity_sha256(candidate)
        now = datetime.now(UTC).isoformat()
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT candidate_json, identity_sha256 FROM model_lab_candidates "
                "WHERE candidate_id = ?",
                (candidate.candidate_id,),
            ).fetchone()
            if row is not None:
                if row["candidate_json"] == payload and row["identity_sha256"] == identity:
                    return
                raise ValueError(
                    f"model candidate identity is immutable: {candidate.candidate_id}"
                )
            conn.execute(
                "INSERT INTO model_lab_candidates("
                "candidate_id, candidate_json, identity_sha256, created_at"
                ") VALUES (?, ?, ?, ?)",
                (candidate.candidate_id, payload, identity, now),
            )

    def get_candidate(self, candidate_id: str) -> ModelCandidate:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT candidate_json, identity_sha256 FROM model_lab_candidates "
                "WHERE candidate_id = ?",
                (candidate_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown model candidate: {candidate_id}")
        candidate = _decode_candidate(row["candidate_json"])
        if candidate_identity_sha256(candidate) != row["identity_sha256"]:
            raise RuntimeError("stored model candidate identity evidence is inconsistent")
        return candidate

    def list_candidates(self) -> tuple[ModelCandidate, ...]:
        with self._store.connection() as conn:
            rows = conn.execute(
                "SELECT candidate_id FROM model_lab_candidates ORDER BY candidate_id"
            ).fetchall()
        return tuple(self.get_candidate(row["candidate_id"]) for row in rows)

    def record_run(self, evidence: BenchmarkRunEvidence) -> None:
        candidate = self.get_candidate(evidence.candidate.candidate_id)
        expected_identity = candidate_identity_sha256(candidate)
        if candidate_identity_sha256(evidence.candidate) != expected_identity:
            raise ValueError("benchmark evidence candidate does not match registered identity")

        document = evidence_document(evidence)
        digest = evidence_sha256(evidence)
        now = datetime.now(UTC).isoformat()
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT evidence_json, evidence_sha256 FROM model_lab_runs WHERE run_id = ?",
                (evidence.run_id,),
            ).fetchone()
            if row is not None:
                if row["evidence_json"] == document and row["evidence_sha256"] == digest:
                    return
                raise ValueError(f"benchmark run evidence is immutable: {evidence.run_id}")
            conn.execute(
                "INSERT INTO model_lab_runs("
                "run_id, candidate_id, candidate_identity_sha256, suite_id, "
                "suite_version, suite_sha256, evidence_json, evidence_sha256, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    evidence.run_id,
                    evidence.candidate.candidate_id,
                    expected_identity,
                    evidence.suite_id,
                    evidence.suite_version,
                    evidence.suite_sha256,
                    document,
                    digest,
                    now,
                ),
            )

    def get_run(self, run_id: str) -> BenchmarkRunEvidence:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT candidate_identity_sha256, evidence_json, evidence_sha256 "
                "FROM model_lab_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"unknown benchmark run: {run_id}")

        raw = str(row["evidence_json"])
        stored_digest = str(row["evidence_sha256"])
        if sha256(raw.encode("utf-8")).hexdigest() != stored_digest:
            raise RuntimeError("stored model benchmark evidence digest is inconsistent")
        evidence = _decode_evidence(raw)
        if evidence_document(evidence) != raw:
            raise RuntimeError("stored model benchmark evidence is not canonical")
        if evidence_sha256(evidence) != stored_digest:
            raise RuntimeError("stored model benchmark evidence digest is inconsistent")
        candidate = self.get_candidate(evidence.candidate.candidate_id)
        identity = candidate_identity_sha256(candidate)
        if identity != row["candidate_identity_sha256"]:
            raise RuntimeError("benchmark run candidate identity no longer matches registry")
        if candidate_identity_sha256(evidence.candidate) != identity:
            raise RuntimeError("benchmark run embeds inconsistent candidate identity")
        return evidence


def _candidate_payload(candidate: ModelCandidate) -> dict[str, object]:
    return {
        "candidate_id": candidate.candidate_id,
        "provider_id": candidate.provider_id,
        "provider_kind": candidate.provider_kind.value,
        "model": candidate.model,
        "model_version": candidate.model_version,
        "license_reference": candidate.license_reference,
        "provenance_reference": candidate.provenance_reference,
        "permission_fingerprint": candidate.permission_fingerprint,
        "artifact_sha256": candidate.artifact_sha256,
    }


def _candidate_document(candidate: ModelCandidate) -> str:
    return json.dumps(
        _candidate_payload(candidate),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_candidate(raw: str) -> ModelCandidate:
    payload = json.loads(raw)
    return ModelCandidate(
        candidate_id=str(payload["candidate_id"]),
        provider_id=str(payload["provider_id"]),
        provider_kind=ProviderKind(str(payload["provider_kind"])),
        model=str(payload["model"]),
        model_version=str(payload["model_version"]),
        license_reference=str(payload["license_reference"]),
        provenance_reference=str(payload["provenance_reference"]),
        permission_fingerprint=str(payload["permission_fingerprint"]),
        artifact_sha256=(
            None if payload["artifact_sha256"] is None else str(payload["artifact_sha256"])
        ),
    )


def _decode_resource(payload: object) -> ResourceSnapshot | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ValueError("invalid resource snapshot in benchmark evidence")
    return ResourceSnapshot(
        cpu_percent=float(payload["cpu_percent"]),
        memory_percent=float(payload["memory_percent"]),
        available_memory_bytes=int(payload["available_memory_bytes"]),
    )


def _decode_evidence(raw: str) -> BenchmarkRunEvidence:
    payload = json.loads(raw)
    if payload.get("schema") != 1:
        raise ValueError("unsupported model benchmark evidence schema")
    candidate_payload = payload["candidate"]
    if not isinstance(candidate_payload, dict):
        raise ValueError("invalid candidate payload in benchmark evidence")
    candidate = _decode_candidate(
        json.dumps(candidate_payload, sort_keys=True, separators=(",", ":"))
    )

    attempts: list[BenchmarkAttempt] = []
    for item in payload["attempts"]:
        attempts.append(
            BenchmarkAttempt(
                attempt_id=str(item["attempt_id"]),
                case_id=str(item["case_id"]),
                repetition=int(item["repetition"]),
                status=AttemptStatus(str(item["status"])),
                request_id=str(item["request_id"]),
                prompt_sha256=str(item["prompt_sha256"]),
                reference_sha256=(
                    None
                    if item["reference_sha256"] is None
                    else str(item["reference_sha256"])
                ),
                provider_id=str(item["provider_id"]),
                provider_kind=ProviderKind(str(item["provider_kind"])),
                model=str(item["model"]),
                wall_latency_ms=float(item["wall_latency_ms"]),
                metrics=tuple(
                    MetricValue(metric=str(metric["metric"]), value=float(metric["value"]))
                    for metric in item["metrics"]
                ),
                response_sha256=(
                    None
                    if item["response_sha256"] is None
                    else str(item["response_sha256"])
                ),
                response_characters=(
                    None
                    if item["response_characters"] is None
                    else int(item["response_characters"])
                ),
                provider_latency_ms=(
                    None
                    if item["provider_latency_ms"] is None
                    else float(item["provider_latency_ms"])
                ),
                input_tokens=(
                    None if item["input_tokens"] is None else int(item["input_tokens"])
                ),
                output_tokens=(
                    None if item["output_tokens"] is None else int(item["output_tokens"])
                ),
                total_tokens=(
                    None if item["total_tokens"] is None else int(item["total_tokens"])
                ),
                resource_before=_decode_resource(item["resource_before"]),
                resource_after=_decode_resource(item["resource_after"]),
                error_code=None if item["error_code"] is None else str(item["error_code"]),
            )
        )
    return BenchmarkRunEvidence(
        run_id=str(payload["run_id"]),
        candidate=candidate,
        suite_id=str(payload["suite_id"]),
        suite_version=str(payload["suite_version"]),
        suite_sha256=str(payload["suite_sha256"]),
        expected_attempts=int(payload["expected_attempts"]),
        attempts=tuple(attempts),
    )
