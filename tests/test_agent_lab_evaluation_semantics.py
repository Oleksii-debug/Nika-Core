from __future__ import annotations

import json
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.experiments import (
    ArtifactKind,
    ExperimentDefinition,
    ExperimentEngine,
    ExperimentStatus,
    InMemoryExperimentRepository,
    MetricObservation,
    PromotionPolicy,
    ReplayCase,
    SQLiteExperimentRepository,
    StrategyRef,
)
from nika_core.multi_agent import EvaluationScore, MultiAgentSupervisor


def _strategy(candidate_id: str) -> StrategyRef:
    return StrategyRef(
        candidate_id=candidate_id,
        version="1",
        artifact_kind=ArtifactKind.STRATEGY,
        artifact_ref=f"strategy://{candidate_id}/1",
        permission_fingerprint="perm-v1",
    )


def _definition(
    *,
    experiment_id: str = "exp-latency",
    primary_higher_is_better: bool = False,
    minimum_improvement: float = 10.0,
) -> ExperimentDefinition:
    return ExperimentDefinition(
        experiment_id=experiment_id,
        champion=_strategy("champion"),
        challengers=(_strategy("challenger-a"), _strategy("challenger-b")),
        replays=(
            ReplayCase("r1", "dataset://latency", "v1"),
            ReplayCase("r2", "dataset://latency", "v1"),
        ),
        policy=PromotionPolicy(
            primary_metric="latency_ms",
            minimum_improvement=minimum_improvement,
            minimum_replays=2,
            primary_higher_is_better=primary_higher_is_better,
        ),
    )


def _record_latency(
    engine: ExperimentEngine,
    experiment_id: str,
    candidate_id: str,
    values: tuple[float, float],
) -> None:
    for replay_id, value in zip(("r1", "r2"), values, strict=True):
        engine.record(
            experiment_id,
            MetricObservation(candidate_id, replay_id, "latency_ms", value),
        )


def test_lower_is_better_primary_metric_promotes_best_eligible_challenger() -> None:
    repository = InMemoryExperimentRepository()
    engine = ExperimentEngine(repository)
    engine.create(_definition())
    engine.start("exp-latency")
    _record_latency(engine, "exp-latency", "champion", (100.0, 100.0))
    _record_latency(engine, "exp-latency", "challenger-a", (85.0, 85.0))
    _record_latency(engine, "exp-latency", "challenger-b", (80.0, 80.0))

    completed = engine.complete("exp-latency")

    assert completed.status is ExperimentStatus.PROMOTED
    assert completed.selected_candidate_id == "challenger-b"


def test_lower_is_better_primary_metric_respects_minimum_improvement() -> None:
    repository = InMemoryExperimentRepository()
    engine = ExperimentEngine(repository)
    engine.create(_definition(minimum_improvement=10.0))
    engine.start("exp-latency")
    _record_latency(engine, "exp-latency", "champion", (100.0, 100.0))
    _record_latency(engine, "exp-latency", "challenger-a", (95.0, 95.0))
    _record_latency(engine, "exp-latency", "challenger-b", (101.0, 101.0))

    completed = engine.complete("exp-latency")

    assert completed.status is ExperimentStatus.COMPLETED
    assert completed.selected_candidate_id == "champion"


def _sqlite_repository(path: Path) -> tuple[SQLiteStore, SQLiteExperimentRepository]:
    store = SQLiteStore(path)
    store.initialize()
    return store, SQLiteExperimentRepository(store)


def test_primary_metric_direction_round_trips_through_sqlite(tmp_path: Path) -> None:
    _, repository = _sqlite_repository(tmp_path / "direction.db")
    engine = ExperimentEngine(repository)
    engine.create(_definition())

    loaded = repository.get("exp-latency")

    assert loaded.definition.policy.primary_higher_is_better is False


def test_legacy_sqlite_definition_defaults_primary_direction_to_higher(tmp_path: Path) -> None:
    store, repository = _sqlite_repository(tmp_path / "legacy.db")
    engine = ExperimentEngine(repository)
    definition = _definition(
        experiment_id="exp-legacy",
        primary_higher_is_better=True,
        minimum_improvement=0.0,
    )
    engine.create(definition)

    with store.connection() as conn:
        row = conn.execute(
            "SELECT definition_json FROM experiments WHERE experiment_id = ?",
            ("exp-legacy",),
        ).fetchone()
        payload = json.loads(row["definition_json"])
        payload["policy"].pop("primary_higher_is_better")
        conn.execute(
            "UPDATE experiments SET definition_json = ? WHERE experiment_id = ?",
            (json.dumps(payload, sort_keys=True, separators=(",", ":")), "exp-legacy"),
        )

    loaded = repository.get("exp-legacy")
    started = engine.start("exp-legacy")

    assert loaded.definition.policy.primary_higher_is_better is True
    assert started.status is ExperimentStatus.RUNNING


def test_evaluator_aggregation_rejects_mixed_metrics() -> None:
    scores = (
        EvaluationScore("e1", "worker", 0.9, metric="quality"),
        EvaluationScore("e2", "worker", 12.0, metric="latency_ms"),
    )

    with pytest.raises(ValueError, match="multiple metrics"):
        MultiAgentSupervisor.aggregate_evaluations(scores)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_evaluator_score_must_be_finite(value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        EvaluationScore("evaluator", "worker", value)
