from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.experiments import (
    ArtifactKind,
    DatasetSplit,
    ExperimentDefinition,
    ExperimentEngine,
    ExperimentStatus,
    InMemoryExperimentRepository,
    MetricObservation,
    MetricRule,
    PromotionPolicy,
    ReplayCase,
    SQLiteExperimentRepository,
    StrategyRef,
)


def _strategy(candidate_id: str) -> StrategyRef:
    return StrategyRef(
        candidate_id=candidate_id,
        version="1",
        artifact_kind=ArtifactKind.PROMPT,
        artifact_ref=f"prompt://{candidate_id}/1",
        permission_fingerprint="perm-v1",
    )


def _definition(*, split: DatasetSplit = DatasetSplit.EVALUATION) -> ExperimentDefinition:
    cutoff = datetime(2026, 8, 1, tzinfo=UTC)
    return ExperimentDefinition(
        experiment_id="exp-authority",
        champion=_strategy("champion"),
        challengers=(_strategy("challenger"),),
        replays=(
            ReplayCase(
                replay_id="r1",
                dataset_ref="dataset://evaluation",
                dataset_version="v1",
                split=split,
                dataset_fingerprint="sha256:eval-v1",
                data_end_at=cutoff,
            ),
        ),
        policy=PromotionPolicy(primary_metric="quality", minimum_improvement=0.05),
        evaluation_cutoff=cutoff,
    )


def _record(engine: ExperimentEngine, *, champion: float, challenger: float) -> None:
    engine.record(
        "exp-authority",
        MetricObservation("champion", "r1", "quality", champion),
    )
    engine.record(
        "exp-authority",
        MetricObservation("challenger", "r1", "quality", challenger),
    )


def _sqlite_repository(path: Path) -> SQLiteExperimentRepository:
    store = SQLiteStore(path)
    store.initialize()
    return SQLiteExperimentRepository(store)


@pytest.mark.parametrize("value", [True, "0.7"])
def test_metric_observation_rejects_bool_and_string_numeric_coercion(value: object) -> None:
    with pytest.raises(TypeError, match="metric observation value must be numeric"):
        MetricObservation("champion", "r1", "quality", value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [True, "0.1"])
def test_metric_rule_rejects_bool_and_string_numeric_coercion(value: object) -> None:
    with pytest.raises(TypeError, match="max_regression must be numeric"):
        MetricRule("safety", max_regression=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [True, "0.1"])
def test_promotion_policy_rejects_bool_and_string_numeric_coercion(value: object) -> None:
    with pytest.raises(TypeError, match="minimum_improvement must be numeric"):
        PromotionPolicy(
            primary_metric="quality",
            minimum_improvement=value,  # type: ignore[arg-type]
        )


def test_promotion_policy_rejects_boolean_minimum_replays() -> None:
    with pytest.raises(TypeError, match="minimum_replays must be an integer"):
        PromotionPolicy(primary_metric="quality", minimum_replays=True)


def test_programmatic_fingerprint_and_cutoff_types_fail_closed() -> None:
    with pytest.raises(TypeError, match="dataset_fingerprint must be a string"):
        ReplayCase(
            "r1",
            "dataset://evaluation",
            "v1",
            dataset_fingerprint=123,  # type: ignore[arg-type]
        )
    with pytest.raises(TypeError, match="evaluation_cutoff must be a datetime"):
        replace(_definition(), evaluation_cutoff="2026-08-01T00:00:00Z")  # type: ignore[arg-type]


def test_inmemory_repository_rejects_caller_fabricated_promotion() -> None:
    repository = InMemoryExperimentRepository()
    engine = ExperimentEngine(repository)
    engine.create(_definition())
    engine.start("exp-authority")
    _record(engine, champion=0.80, challenger=0.70)
    running = repository.get("exp-authority")

    with pytest.raises(ValueError, match="terminal status conflicts"):
        repository.save(
            replace(
                running,
                status=ExperimentStatus.PROMOTED,
                selected_candidate_id="challenger",
                previous_champion_id="champion",
            )
        )


def test_sqlite_repository_rejects_caller_fabricated_promotion(tmp_path: Path) -> None:
    repository = _sqlite_repository(tmp_path / "nika.db")
    engine = ExperimentEngine(repository)
    engine.create(_definition())
    engine.start("exp-authority")
    _record(engine, champion=0.80, challenger=0.70)
    running = repository.get("exp-authority")

    with pytest.raises(ValueError, match="terminal status conflicts"):
        repository.save(
            replace(
                running,
                status=ExperimentStatus.PROMOTED,
                selected_candidate_id="challenger",
                previous_champion_id="champion",
            )
        )


def test_evaluation_split_can_still_promote_and_survive_restart(tmp_path: Path) -> None:
    path = tmp_path / "nika.db"
    repository = _sqlite_repository(path)
    engine = ExperimentEngine(repository)
    definition = _definition(split=DatasetSplit.EVALUATION)
    engine.create(definition)
    engine.start("exp-authority")
    _record(engine, champion=0.70, challenger=0.80)

    promoted = engine.complete("exp-authority")
    assert promoted.status is ExperimentStatus.PROMOTED
    assert promoted.selected_candidate_id == "challenger"
    recovered = _sqlite_repository(path).get("exp-authority")
    assert recovered == promoted
    assert recovered.definition.replays[0].split is DatasetSplit.EVALUATION


def test_repository_rejects_fabricated_rollback_target() -> None:
    repository = InMemoryExperimentRepository()
    engine = ExperimentEngine(repository)
    engine.create(_definition())
    engine.start("exp-authority")
    _record(engine, champion=0.70, challenger=0.80)
    promoted = engine.complete("exp-authority")

    with pytest.raises(ValueError, match="rollback must restore"):
        repository.save(
            replace(
                promoted,
                status=ExperimentStatus.ROLLED_BACK,
                selected_candidate_id="challenger",
            )
        )


def test_sqlite_restart_detects_terminal_decision_tamper(tmp_path: Path) -> None:
    path = tmp_path / "nika.db"
    store = SQLiteStore(path)
    store.initialize()
    repository = SQLiteExperimentRepository(store)
    engine = ExperimentEngine(repository)
    engine.create(_definition())
    engine.start("exp-authority")
    _record(engine, champion=0.70, challenger=0.80)
    engine.complete("exp-authority")

    with store.connection() as conn:
        conn.execute(
            "UPDATE experiments SET selected_candidate_id = ? WHERE experiment_id = ?",
            ("champion", "exp-authority"),
        )

    restarted = _sqlite_repository(path)
    with pytest.raises(ValueError, match="selected candidate conflicts"):
        restarted.get("exp-authority")


def test_terminal_transition_cannot_smuggle_new_evidence() -> None:
    repository = InMemoryExperimentRepository()
    engine = ExperimentEngine(repository)
    engine.create(_definition())
    running = engine.start("exp-authority")
    proposed = replace(
        running,
        observations=(
            MetricObservation("champion", "r1", "quality", 0.70),
            MetricObservation("challenger", "r1", "quality", 0.80),
        ),
        status=ExperimentStatus.PROMOTED,
        selected_candidate_id="challenger",
        previous_champion_id="champion",
    )

    with pytest.raises(ValueError, match="evidence may only be appended while remaining running"):
        repository.save(proposed)
