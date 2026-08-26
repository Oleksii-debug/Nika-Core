from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.model_engineering import (
    BenchmarkMetric,
    BenchmarkObservation,
    BenchmarkSuite,
    EvidenceIntegrityError,
    MetricDirection,
    MetricObservation,
    ModelCandidate,
    ModelEngineeringLab,
    SQLiteModelEngineeringRepository,
)

NOW = datetime(2026, 8, 26, 21, 0, tzinfo=UTC)
INPUT_DIGEST = "a" * 64
OUTPUT_DIGEST = "b" * 64


def _suite() -> BenchmarkSuite:
    return BenchmarkSuite(
        suite_id="mel01-adversarial",
        version="1",
        dataset_sha256="d" * 64,
        metrics=(
            BenchmarkMetric(
                name="quality",
                direction=MetricDirection.MAXIMIZE,
                weight=1.0,
                worst_value=0.0,
                best_value=1.0,
            ),
        ),
        required_case_ids=("case-a", "case-b"),
    )


def _observation(*, observation_id: str, case_id: str) -> BenchmarkObservation:
    return BenchmarkObservation(
        observation_id=observation_id,
        run_id="run-qa",
        suite_id="mel01-adversarial",
        suite_version="1",
        candidate=ModelCandidate("local", "qa-model", "immutable-revision"),
        case_id=case_id,
        input_sha256=INPUT_DIGEST,
        output_sha256=OUTPUT_DIGEST,
        metrics=(MetricObservation("quality", 0.75),),
        observed_at=NOW,
    )


def _lab(tmp_path: Path) -> tuple[ModelEngineeringLab, SQLiteStore]:
    store = SQLiteStore(tmp_path / "Nika QA" / "mel.sqlite3")
    repository = SQLiteModelEngineeringRepository(store)
    service = ModelEngineeringLab(repository)
    service.initialize()
    service.register_suite(_suite())
    return service, store


def test_exact_observation_replay_remains_idempotent_after_run_is_sealed(
    tmp_path: Path,
) -> None:
    service, store = _lab(tmp_path)
    first = _observation(observation_id="obs-a", case_id="case-a")
    second = _observation(observation_id="obs-b", case_id="case-b")
    service.record_observation(first)
    service.record_observation(second)
    service.recommend("run-qa", _suite().key, created_at=NOW)

    # A sealed run must block genuinely new evidence, but an exact replay is not
    # new evidence. Durable retry/restart semantics require the same write to
    # converge to the row that already exists rather than fail because sealing
    # is checked before exact-replay identity.
    service.record_observation(first)

    repository = SQLiteModelEngineeringRepository(store)
    assert repository.list_observations("run-qa", _suite().key) == (first, second)


def test_observation_index_identity_drift_fails_closed(tmp_path: Path) -> None:
    service, store = _lab(tmp_path)
    first = _observation(observation_id="obs-index", case_id="case-a")
    service.record_observation(first)

    # payload_json and payload_sha256 remain canonical and untouched. Only the
    # denormalized identity column used by uniqueness/ordering is corrupted.
    # Returning the payload anyway would let index identity diverge from the
    # durable evidence identity and can reopen semantic duplicate slots.
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE model_engineering_observations SET candidate_key = ? "
            "WHERE observation_id = ?",
            ("0" * 64, first.observation_id),
        )

    repository = SQLiteModelEngineeringRepository(store)
    with pytest.raises(EvidenceIntegrityError):
        repository.list_observations("run-qa", _suite().key)


def test_observation_run_selector_rebinding_fails_closed(tmp_path: Path) -> None:
    service, store = _lab(tmp_path)
    first = _observation(observation_id="obs-run-selector", case_id="case-a")
    service.record_observation(first)

    # Rebind only the query/index column. The canonical payload remains bound to
    # run-qa. A forged selector must not make that original evidence appear as
    # evidence for a different benchmark run.
    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE model_engineering_observations SET run_id = ? "
            "WHERE observation_id = ?",
            ("forged-run", first.observation_id),
        )

    repository = SQLiteModelEngineeringRepository(store)
    with pytest.raises(EvidenceIntegrityError):
        repository.list_observations("forged-run", _suite().key)


def test_schema_version_marker_without_required_tables_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "Unicode каталог" / "MEL state.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE model_engineering_schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO model_engineering_schema_migrations(version, applied_at) "
            "VALUES (?, ?)",
            (1, NOW.isoformat()),
        )

    repository = SQLiteModelEngineeringRepository(SQLiteStore(path))

    # A version marker is not sufficient authority for a usable schema. A
    # crash/tamper state that says v1 while required MEL tables are absent must
    # be rejected during restart initialization rather than accepted as healthy.
    with pytest.raises((RuntimeError, EvidenceIntegrityError)):
        repository.initialize()
