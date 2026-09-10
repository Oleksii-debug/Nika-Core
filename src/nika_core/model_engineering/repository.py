from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from nika_core.data.sqlite import SQLiteStore
from nika_core.model_engineering.contracts import (
    BenchmarkObservation,
    BenchmarkRecommendation,
    BenchmarkSuite,
    canonical_json,
)
from nika_core.model_engineering.scoring import rank_benchmark_candidates

MODEL_ENGINEERING_SCHEMA_VERSION = 1

_MODEL_ENGINEERING_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        "CREATE TABLE IF NOT EXISTS model_engineering_suites ("
        "suite_key TEXT PRIMARY KEY, payload_json TEXT NOT NULL, "
        "payload_sha256 TEXT NOT NULL, created_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS model_engineering_observations ("
        "observation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, suite_key TEXT NOT NULL, "
        "candidate_key TEXT NOT NULL, case_id TEXT NOT NULL, payload_json TEXT NOT NULL, "
        "payload_sha256 TEXT NOT NULL, created_at TEXT NOT NULL, "
        "FOREIGN KEY(suite_key) REFERENCES model_engineering_suites(suite_key), "
        "UNIQUE(run_id, suite_key, candidate_key, case_id))",
        "CREATE TABLE IF NOT EXISTS model_engineering_recommendations ("
        "recommendation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, suite_key TEXT NOT NULL, "
        "payload_json TEXT NOT NULL, payload_sha256 TEXT NOT NULL, created_at TEXT NOT NULL, "
        "FOREIGN KEY(suite_key) REFERENCES model_engineering_suites(suite_key), "
        "UNIQUE(run_id, suite_key))",
    )
}


class EvidenceConflictError(RuntimeError):
    """Raised when an immutable benchmark identity is replayed with different content."""


class EvidenceIntegrityError(RuntimeError):
    """Raised when persisted canonical payload bytes no longer match their digest."""


class BenchmarkRunSealedError(RuntimeError):
    """Raised when evidence is appended after a recommendation sealed the run."""


def _digest_json(payload_json: str) -> str:
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def _decode_verified_payload(payload_json: str, expected_sha256: str) -> dict[str, Any]:
    if _digest_json(payload_json) != expected_sha256:
        raise EvidenceIntegrityError("persisted benchmark evidence digest mismatch")
    payload = json.loads(payload_json)
    if not isinstance(payload, dict):
        raise EvidenceIntegrityError("persisted benchmark evidence is not a JSON object")
    if canonical_json(payload) != payload_json:
        raise EvidenceIntegrityError("persisted benchmark evidence is not canonical JSON")
    return payload


class SQLiteModelEngineeringRepository:
    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def initialize(self) -> None:
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS model_engineering_schema_migrations ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            row = conn.execute(
                "SELECT MAX(version) AS version FROM model_engineering_schema_migrations"
            ).fetchone()
            current = int(row["version"] or 0)
            if current > MODEL_ENGINEERING_SCHEMA_VERSION:
                raise RuntimeError(
                    "model engineering database schema "
                    f"{current} is newer than supported schema {MODEL_ENGINEERING_SCHEMA_VERSION}"
                )
            for version in range(current + 1, MODEL_ENGINEERING_SCHEMA_VERSION + 1):
                statements = _MODEL_ENGINEERING_MIGRATIONS.get(version)
                if statements is None:
                    raise RuntimeError(f"missing model engineering migration {version}")
                for statement in statements:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO model_engineering_schema_migrations(version, applied_at) "
                    "VALUES (?, ?)",
                    (version, datetime.now(UTC).isoformat()),
                )

    def save_suite(self, suite: BenchmarkSuite) -> None:
        payload_json = canonical_json(suite.to_payload())
        digest = _digest_json(payload_json)
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "INSERT OR IGNORE INTO model_engineering_suites("
                "suite_key, payload_json, payload_sha256, created_at) VALUES (?, ?, ?, ?)",
                (suite.key, payload_json, digest, datetime.now(UTC).isoformat()),
            )
            if cursor.rowcount == 1:
                return
            existing = conn.execute(
                "SELECT payload_json, payload_sha256 FROM model_engineering_suites "
                "WHERE suite_key = ?",
                (suite.key,),
            ).fetchone()
            if existing is None:
                raise EvidenceConflictError("benchmark suite insert lost without a persisted row")
            _decode_verified_payload(existing["payload_json"], existing["payload_sha256"])
            if existing["payload_sha256"] != digest:
                raise EvidenceConflictError(
                    "benchmark suite identity already exists with different payload: "
                    f"{suite.key}"
                )

    def get_suite(self, suite_key: str) -> BenchmarkSuite | None:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT payload_json, payload_sha256 FROM model_engineering_suites "
                "WHERE suite_key = ?",
                (suite_key,),
            ).fetchone()
        if row is None:
            return None
        payload = _decode_verified_payload(row["payload_json"], row["payload_sha256"])
        suite = BenchmarkSuite.from_payload(payload)
        if suite.key != suite_key:
            raise EvidenceIntegrityError("suite key does not match persisted payload")
        return suite

    def save_observation(self, observation: BenchmarkObservation) -> None:
        payload_json = canonical_json(observation.to_payload())
        digest = _digest_json(payload_json)
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            suite = conn.execute(
                "SELECT 1 FROM model_engineering_suites WHERE suite_key = ?",
                (observation.suite_key,),
            ).fetchone()
            if suite is None:
                raise KeyError(f"unknown benchmark suite: {observation.suite_key}")
            sealed = conn.execute(
                "SELECT 1 FROM model_engineering_recommendations "
                "WHERE run_id = ? AND suite_key = ?",
                (observation.run_id, observation.suite_key),
            ).fetchone()
            if sealed is not None:
                raise BenchmarkRunSealedError(
                    f"benchmark run is sealed by a recommendation: {observation.run_id}"
                )
            cursor = conn.execute(
                "INSERT OR IGNORE INTO model_engineering_observations("
                "observation_id, run_id, suite_key, candidate_key, case_id, payload_json, "
                "payload_sha256, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    observation.observation_id,
                    observation.run_id,
                    observation.suite_key,
                    observation.candidate.key,
                    observation.case_id,
                    payload_json,
                    digest,
                    datetime.now(UTC).isoformat(),
                ),
            )
            if cursor.rowcount == 1:
                return
            existing = conn.execute(
                "SELECT payload_json, payload_sha256 FROM model_engineering_observations "
                "WHERE observation_id = ?",
                (observation.observation_id,),
            ).fetchone()
            if existing is not None:
                _decode_verified_payload(existing["payload_json"], existing["payload_sha256"])
                if existing["payload_sha256"] == digest:
                    return
                raise EvidenceConflictError(
                    "observation_id already exists with different benchmark evidence"
                )
            semantic_existing = conn.execute(
                "SELECT observation_id, payload_json, payload_sha256 "
                "FROM model_engineering_observations "
                "WHERE run_id = ? AND suite_key = ? AND candidate_key = ? AND case_id = ?",
                (
                    observation.run_id,
                    observation.suite_key,
                    observation.candidate.key,
                    observation.case_id,
                ),
            ).fetchone()
            if semantic_existing is not None:
                _decode_verified_payload(
                    semantic_existing["payload_json"], semantic_existing["payload_sha256"]
                )
                raise EvidenceConflictError(
                    "candidate/case benchmark evidence already exists under observation_id "
                    f"{semantic_existing['observation_id']}"
                )
            raise EvidenceConflictError("observation insert was ignored for an unknown conflict")

    def list_observations(self, run_id: str, suite_key: str) -> tuple[BenchmarkObservation, ...]:
        with self._store.connection() as conn:
            rows = conn.execute(
                "SELECT observation_id, payload_json, payload_sha256 "
                "FROM model_engineering_observations WHERE run_id = ? AND suite_key = ? "
                "ORDER BY candidate_key, case_id, observation_id",
                (run_id, suite_key),
            ).fetchall()
        observations: list[BenchmarkObservation] = []
        for row in rows:
            payload = _decode_verified_payload(row["payload_json"], row["payload_sha256"])
            observation = BenchmarkObservation.from_payload(payload)
            if observation.observation_id != row["observation_id"]:
                raise EvidenceIntegrityError(
                    "observation identity does not match persisted payload"
                )
            observations.append(observation)
        return tuple(observations)

    def save_recommendation(self, recommendation: BenchmarkRecommendation) -> None:
        payload_json = canonical_json(recommendation.to_payload())
        digest = _digest_json(payload_json)
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            suite_row = conn.execute(
                "SELECT payload_json, payload_sha256 FROM model_engineering_suites "
                "WHERE suite_key = ?",
                (recommendation.suite_key,),
            ).fetchone()
            if suite_row is None:
                raise KeyError(f"unknown benchmark suite: {recommendation.suite_key}")
            suite_payload = _decode_verified_payload(
                suite_row["payload_json"], suite_row["payload_sha256"]
            )
            suite = BenchmarkSuite.from_payload(suite_payload)
            observation_rows = conn.execute(
                "SELECT payload_json, payload_sha256 FROM model_engineering_observations "
                "WHERE run_id = ? AND suite_key = ? ORDER BY payload_sha256",
                (recommendation.run_id, recommendation.suite_key),
            ).fetchall()
            current_source = tuple(row["payload_sha256"] for row in observation_rows)
            if current_source != recommendation.source_observation_sha256:
                raise EvidenceConflictError(
                    "benchmark observation set changed before recommendation commit"
                )
            observations = tuple(
                BenchmarkObservation.from_payload(
                    _decode_verified_payload(row["payload_json"], row["payload_sha256"])
                )
                for row in observation_rows
            )
            expected = rank_benchmark_candidates(
                suite,
                observations,
                created_at=recommendation.created_at,
            )
            if expected != recommendation:
                raise EvidenceConflictError(
                    "recommendation does not match deterministic benchmark scoring"
                )
            cursor = conn.execute(
                "INSERT OR IGNORE INTO model_engineering_recommendations("
                "recommendation_id, run_id, suite_key, payload_json, payload_sha256, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    recommendation.recommendation_id,
                    recommendation.run_id,
                    recommendation.suite_key,
                    payload_json,
                    digest,
                    datetime.now(UTC).isoformat(),
                ),
            )
            if cursor.rowcount == 1:
                return
            existing = conn.execute(
                "SELECT recommendation_id, payload_json, payload_sha256 "
                "FROM model_engineering_recommendations WHERE run_id = ? AND suite_key = ?",
                (recommendation.run_id, recommendation.suite_key),
            ).fetchone()
            if existing is None:
                raise EvidenceConflictError(
                    "recommendation insert lost without a persisted recommendation"
                )
            _decode_verified_payload(existing["payload_json"], existing["payload_sha256"])
            if (
                existing["recommendation_id"] != recommendation.recommendation_id
                or existing["payload_sha256"] != digest
            ):
                raise EvidenceConflictError(
                    "benchmark run already has a different persisted recommendation"
                )

    def get_recommendation(
        self, run_id: str, suite_key: str
    ) -> BenchmarkRecommendation | None:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT recommendation_id, payload_json, payload_sha256 "
                "FROM model_engineering_recommendations WHERE run_id = ? AND suite_key = ?",
                (run_id, suite_key),
            ).fetchone()
        if row is None:
            return None
        payload = _decode_verified_payload(row["payload_json"], row["payload_sha256"])
        recommendation = BenchmarkRecommendation.from_payload(payload)
        if recommendation.recommendation_id != row["recommendation_id"]:
            raise EvidenceIntegrityError(
                "recommendation identity does not match persisted payload"
            )
        return recommendation

    def schema_version(self) -> int:
        with self._store.connection() as conn:
            try:
                row = conn.execute(
                    "SELECT MAX(version) AS version FROM model_engineering_schema_migrations"
                ).fetchone()
            except sqlite3.OperationalError:
                return 0
        return int(row["version"] or 0)
