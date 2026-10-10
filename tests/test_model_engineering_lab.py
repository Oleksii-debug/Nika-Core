from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.model_engineering import (
    BenchmarkMetric,
    BenchmarkObservation,
    BenchmarkRunSealedError,
    BenchmarkSuite,
    EvidenceConflictError,
    EvidenceIntegrityError,
    MetricDirection,
    MetricObservation,
    ModelCandidate,
    ModelEngineeringLab,
    SQLiteModelEngineeringRepository,
)

DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
NOW = datetime(2026, 8, 26, 20, 0, tzinfo=UTC)


def suite() -> BenchmarkSuite:
    return BenchmarkSuite(
        suite_id="core-agent-quality",
        version="2026.08",
        dataset_sha256="d" * 64,
        metrics=(
            BenchmarkMetric(
                name="quality",
                direction=MetricDirection.MAXIMIZE,
                weight=3.0,
                worst_value=0.0,
                best_value=1.0,
            ),
            BenchmarkMetric(
                name="latency_ms",
                direction=MetricDirection.MINIMIZE,
                weight=1.0,
                worst_value=2000.0,
                best_value=200.0,
            ),
        ),
        required_case_ids=("case-a", "case-b"),
    )


def observation(
    *,
    observation_id: str,
    candidate: ModelCandidate,
    case_id: str,
    quality: float,
    latency_ms: float,
    run_id: str = "run-001",
) -> BenchmarkObservation:
    return BenchmarkObservation(
        observation_id=observation_id,
        run_id=run_id,
        suite_id="core-agent-quality",
        suite_version="2026.08",
        candidate=candidate,
        case_id=case_id,
        input_sha256=DIGEST_A,
        output_sha256=DIGEST_B,
        metrics=(
            MetricObservation("quality", quality),
            MetricObservation("latency_ms", latency_ms),
        ),
        observed_at=NOW,
    )


def lab(tmp_path: Path) -> tuple[ModelEngineeringLab, SQLiteStore]:
    store = SQLiteStore(tmp_path / "nika core" / "benchmarks.sqlite3")
    repository = SQLiteModelEngineeringRepository(store)
    service = ModelEngineeringLab(repository)
    service.initialize()
    service.register_suite(suite())
    return service, store


def test_ranking_is_deterministic_and_review_only(tmp_path: Path) -> None:
    service, _ = lab(tmp_path)
    fast = ModelCandidate("ollama", "qwen3:8b", "sha256-fast")
    accurate = ModelCandidate("foundry-local", "phi", "build-42")

    for item in (
        observation(
            observation_id="o1",
            candidate=fast,
            case_id="case-a",
            quality=0.80,
            latency_ms=300,
        ),
        observation(
            observation_id="o2",
            candidate=fast,
            case_id="case-b",
            quality=0.80,
            latency_ms=300,
        ),
        observation(
            observation_id="o3",
            candidate=accurate,
            case_id="case-a",
            quality=0.95,
            latency_ms=1000,
        ),
        observation(
            observation_id="o4",
            candidate=accurate,
            case_id="case-b",
            quality=0.95,
            latency_ms=1000,
        ),
    ):
        service.record_observation(item)

    result = service.recommend("run-001", suite().key, created_at=NOW)

    assert result.winner.candidate == accurate
    assert result.requires_human_review is True
    assert result.promotion_allowed is False
    assert result.recommendation_id == "mel-" + result.evidence_sha256[:24]

    reopened = ModelEngineeringLab(
        SQLiteModelEngineeringRepository(SQLiteStore(tmp_path / "nika core" / "benchmarks.sqlite3"))
    )
    persisted = reopened.get_recommendation("run-001", suite().key)
    assert persisted == result


def test_incomplete_candidate_is_excluded_and_cannot_win(tmp_path: Path) -> None:
    service, _ = lab(tmp_path)
    complete = ModelCandidate("local", "complete")
    incomplete = ModelCandidate("local", "incomplete")
    for item in (
        observation(
            observation_id="complete-a",
            candidate=complete,
            case_id="case-a",
            quality=0.4,
            latency_ms=900,
        ),
        observation(
            observation_id="complete-b",
            candidate=complete,
            case_id="case-b",
            quality=0.4,
            latency_ms=900,
        ),
        observation(
            observation_id="incomplete-a",
            candidate=incomplete,
            case_id="case-a",
            quality=1.0,
            latency_ms=200,
        ),
    ):
        service.record_observation(item)

    result = service.recommend("run-001", suite().key, created_at=NOW)

    assert result.winner.candidate == complete
    assert result.excluded_candidates[0].candidate == incomplete
    assert result.excluded_candidates[0].reason == "missing_required_cases:case-b"


def test_observation_replay_is_idempotent_but_conflict_fails_closed(tmp_path: Path) -> None:
    service, _ = lab(tmp_path)
    candidate = ModelCandidate("ollama", "qwen3:8b")
    original = observation(
        observation_id="obs-idempotent",
        candidate=candidate,
        case_id="case-a",
        quality=0.8,
        latency_ms=400,
    )
    service.record_observation(original)
    service.record_observation(original)

    conflicting = observation(
        observation_id="obs-idempotent",
        candidate=candidate,
        case_id="case-a",
        quality=0.9,
        latency_ms=400,
    )
    with pytest.raises(EvidenceConflictError):
        service.record_observation(conflicting)

    duplicate_semantic_identity = observation(
        observation_id="different-id",
        candidate=candidate,
        case_id="case-a",
        quality=0.8,
        latency_ms=400,
    )
    with pytest.raises(EvidenceConflictError):
        service.record_observation(duplicate_semantic_identity)


def test_recommendation_seals_run_against_late_evidence(tmp_path: Path) -> None:
    service, _ = lab(tmp_path)
    candidate = ModelCandidate("local", "stable")
    service.record_observation(
        observation(
            observation_id="stable-a",
            candidate=candidate,
            case_id="case-a",
            quality=0.7,
            latency_ms=500,
        )
    )
    service.record_observation(
        observation(
            observation_id="stable-b",
            candidate=candidate,
            case_id="case-b",
            quality=0.7,
            latency_ms=500,
        )
    )
    service.recommend("run-001", suite().key, created_at=NOW)

    with pytest.raises(BenchmarkRunSealedError):
        service.record_observation(
            observation(
                observation_id="late",
                candidate=ModelCandidate("local", "late"),
                case_id="case-a",
                quality=1.0,
                latency_ms=200,
            )
        )


def test_persisted_payload_tamper_is_detected(tmp_path: Path) -> None:
    service, store = lab(tmp_path)
    candidate = ModelCandidate("local", "stable")
    service.record_observation(
        observation(
            observation_id="tamper",
            candidate=candidate,
            case_id="case-a",
            quality=0.7,
            latency_ms=500,
        )
    )

    with sqlite3.connect(store.path) as conn:
        conn.execute(
            "UPDATE model_engineering_observations SET payload_json = ? WHERE observation_id = ?",
            ("{}", "tamper"),
        )

    repository = SQLiteModelEngineeringRepository(store)
    with pytest.raises(EvidenceIntegrityError):
        repository.list_observations("run-001", suite().key)


def test_validation_rejects_nonfinite_metrics_and_secret_shaped_identity() -> None:
    with pytest.raises(ValueError):
        MetricObservation("quality", float("nan"))
    with pytest.raises(ValueError):
        ModelCandidate("sk-secret-looking-value", "model")


def test_schema_is_independently_versioned_and_restart_safe(tmp_path: Path) -> None:
    path = tmp_path / "Unicode каталог" / "Nika Core.sqlite3"
    first = SQLiteModelEngineeringRepository(SQLiteStore(path))
    first.initialize()
    first.save_suite(suite())
    assert first.schema_version() == 1

    second = SQLiteModelEngineeringRepository(SQLiteStore(path))
    second.initialize()
    assert second.schema_version() == 1
    assert second.get_suite(suite().key) == suite()


def test_recommend_is_idempotent_after_run_is_sealed(tmp_path: Path) -> None:
    service, _ = lab(tmp_path)
    candidate = ModelCandidate("local", "repeatable")
    for case_id, suffix in (("case-a", "a"), ("case-b", "b")):
        service.record_observation(
            observation(
                observation_id=f"repeat-{suffix}",
                candidate=candidate,
                case_id=case_id,
                quality=0.75,
                latency_ms=450,
            )
        )

    first = service.recommend("run-001", suite().key)
    second = service.recommend("run-001", suite().key)

    assert second == first
    assert second.source_observation_sha256 == tuple(sorted(second.source_observation_sha256))


def test_recommendation_commit_rejects_changed_source_set(tmp_path: Path) -> None:
    service, store = lab(tmp_path)
    repository = SQLiteModelEngineeringRepository(store)
    first = ModelCandidate("local", "first")
    for case_id, suffix in (("case-a", "a"), ("case-b", "b")):
        service.record_observation(
            observation(
                observation_id=f"first-{suffix}",
                candidate=first,
                case_id=case_id,
                quality=0.6,
                latency_ms=600,
            )
        )

    from nika_core.model_engineering.scoring import rank_benchmark_candidates

    stale = rank_benchmark_candidates(
        suite(),
        repository.list_observations("run-001", suite().key),
        created_at=NOW,
    )
    service.record_observation(
        observation(
            observation_id="late-before-seal",
            candidate=ModelCandidate("local", "incomplete-late"),
            case_id="case-a",
            quality=1.0,
            latency_ms=200,
        )
    )

    with pytest.raises(EvidenceConflictError, match="observation set changed"):
        repository.save_recommendation(stale)


def test_concurrent_exact_observation_replay_converges_to_one_row(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    service, store = lab(tmp_path)
    candidate = ModelCandidate("ollama", "qwen3:8b")
    item = observation(
        observation_id="concurrent-replay",
        candidate=candidate,
        case_id="case-a",
        quality=0.8,
        latency_ms=400,
    )

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(service.record_observation, item) for _ in range(16)]
        for future in futures:
            future.result()

    repository = SQLiteModelEngineeringRepository(store)
    rows = repository.list_observations("run-001", suite().key)
    assert rows == (item,)


def test_persisted_observation_contains_digests_not_raw_model_io(tmp_path: Path) -> None:
    service, store = lab(tmp_path)
    candidate = ModelCandidate("local", "privacy-check")
    service.record_observation(
        observation(
            observation_id="privacy",
            candidate=candidate,
            case_id="case-a",
            quality=0.5,
            latency_ms=700,
        )
    )

    with sqlite3.connect(store.path) as conn:
        payload_json = conn.execute(
            "SELECT payload_json FROM model_engineering_observations WHERE observation_id = ?",
            ("privacy",),
        ).fetchone()[0]

    assert '"input_sha256"' in payload_json
    assert '"output_sha256"' in payload_json
    assert '"prompt"' not in payload_json
    assert '"output_text"' not in payload_json


def test_repository_rejects_forged_recommendation_even_with_matching_source_set(
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    from nika_core.model_engineering.scoring import rank_benchmark_candidates

    service, store = lab(tmp_path)
    repository = SQLiteModelEngineeringRepository(store)
    candidate = ModelCandidate("local", "forgery-check")
    for case_id, suffix in (("case-a", "a"), ("case-b", "b")):
        service.record_observation(
            observation(
                observation_id=f"forgery-{suffix}",
                candidate=candidate,
                case_id=case_id,
                quality=0.8,
                latency_ms=400,
            )
        )
    valid = rank_benchmark_candidates(
        suite(),
        repository.list_observations("run-001", suite().key),
        created_at=NOW,
    )
    forged = replace(valid, evidence_sha256="0" * 64)

    with pytest.raises(EvidenceConflictError, match="deterministic benchmark scoring"):
        repository.save_recommendation(forged)
