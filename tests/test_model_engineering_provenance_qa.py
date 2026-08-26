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
    EvidenceConflictError,
    EvidenceIntegrityError,
    MetricDirection,
    MetricObservation,
    ModelCandidate,
    ModelEngineeringLab,
    SQLiteModelEngineeringRepository,
)

_NOW = datetime(2026, 8, 26, 20, 30, tzinfo=UTC)
_SUITE_DATASET_SHA256 = "d" * 64
_INPUT_A_SHA256 = "a" * 64
_INPUT_B_SHA256 = "b" * 64
_OUTPUT_SHA256 = "c" * 64


def _suite(*, suite_id: str = "qa-benchmark", version: str = "1") -> BenchmarkSuite:
    return BenchmarkSuite(
        suite_id=suite_id,
        version=version,
        dataset_sha256=_SUITE_DATASET_SHA256,
        metrics=(
            BenchmarkMetric(
                name="quality",
                direction=MetricDirection.MAXIMIZE,
                weight=1.0,
                worst_value=0.0,
                best_value=1.0,
            ),
        ),
        required_case_ids=("case-a",),
    )


def _observation(
    *,
    observation_id: str,
    candidate: ModelCandidate,
    input_sha256: str,
    run_id: str = "run-qa",
    suite_id: str = "qa-benchmark",
    suite_version: str = "1",
) -> BenchmarkObservation:
    return BenchmarkObservation(
        observation_id=observation_id,
        run_id=run_id,
        suite_id=suite_id,
        suite_version=suite_version,
        candidate=candidate,
        case_id="case-a",
        input_sha256=input_sha256,
        output_sha256=_OUTPUT_SHA256,
        metrics=(MetricObservation(name="quality", value=0.8),),
        observed_at=_NOW,
    )


def _lab(tmp_path: Path) -> tuple[ModelEngineeringLab, SQLiteStore]:
    store = SQLiteStore(tmp_path / "MEL QA" / "evidence.sqlite3")
    repository = SQLiteModelEngineeringRepository(store)
    service = ModelEngineeringLab(repository)
    service.initialize()
    service.register_suite(_suite())
    return service, store


def test_same_benchmark_case_rejects_cross_candidate_input_drift(tmp_path: Path) -> None:
    """One suite/run/case must not compare candidates against different inputs."""
    service, _ = _lab(tmp_path)
    service.record_observation(
        _observation(
            observation_id="obs-a",
            candidate=ModelCandidate("local", "candidate-a", "revision-a"),
            input_sha256=_INPUT_A_SHA256,
        )
    )

    conflicting_input = _observation(
        observation_id="obs-b",
        candidate=ModelCandidate("local", "candidate-b", "revision-b"),
        input_sha256=_INPUT_B_SHA256,
    )

    with pytest.raises((EvidenceConflictError, ValueError)):
        service.record_observation(conflicting_input)


def test_recommendation_row_run_rebinding_fails_closed(tmp_path: Path) -> None:
    """Indexed SQLite identity must stay bound to the canonical recommendation payload."""
    service, store = _lab(tmp_path)
    service.record_observation(
        _observation(
            observation_id="obs-sealed",
            candidate=ModelCandidate("local", "candidate", "revision-1"),
            input_sha256=_INPUT_A_SHA256,
        )
    )
    recommendation = service.recommend("run-qa", _suite().key, created_at=_NOW)

    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE model_engineering_recommendations SET run_id = ? "
            "WHERE recommendation_id = ?",
            ("run-rebound", recommendation.recommendation_id),
        )
        conn.commit()

    with pytest.raises(EvidenceIntegrityError):
        service.get_recommendation("run-rebound", _suite().key)
