from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nika_core.experiments.contracts import (
    ArtifactKind,
    DatasetSplit,
    ExperimentDefinition,
    ExperimentStatus,
    MetricObservation,
    PromotionPolicy,
    ReplayCase,
    StrategyRef,
)
from nika_core.experiments.engine import ExperimentEngine
from nika_core.experiments.repository import InMemoryExperimentRepository


def _strategy(candidate_id: str) -> StrategyRef:
    return StrategyRef(
        candidate_id=candidate_id,
        version="1",
        artifact_kind=ArtifactKind.PROMPT,
        artifact_ref=f"prompt://{candidate_id}/1",
        permission_fingerprint="perm-v1",
    )


def _definition() -> ExperimentDefinition:
    cutoff = datetime(2026, 9, 1, tzinfo=UTC)
    return ExperimentDefinition(
        experiment_id="exp-strategy-promotion-guard",
        champion=_strategy("champion"),
        challengers=(_strategy("challenger"),),
        replays=(
            ReplayCase(
                replay_id="r1",
                dataset_ref="dataset://held-out",
                dataset_version="v1",
                split=DatasetSplit.EVALUATION,
                dataset_fingerprint="sha256:held-out-v1",
                data_end_at=cutoff,
            ),
        ),
        policy=PromotionPolicy(
            primary_metric="quality",
            minimum_improvement=0.05,
            minimum_replays=1,
        ),
        evaluation_cutoff=cutoff,
    )


def _running_engine() -> tuple[ExperimentEngine, InMemoryExperimentRepository]:
    repository = InMemoryExperimentRepository()
    engine = ExperimentEngine(repository)
    engine.create(_definition())
    engine.start("exp-strategy-promotion-guard")
    return engine, repository


def test_completion_recommends_challenger_without_claiming_promotion_authority() -> None:
    """Benchmark evidence may recommend a challenger; it may not promote it."""

    engine, _repository = _running_engine()
    engine.record(
        "exp-strategy-promotion-guard",
        MetricObservation("champion", "r1", "quality", 0.70),
    )
    engine.record(
        "exp-strategy-promotion-guard",
        MetricObservation("challenger", "r1", "quality", 0.80),
    )

    completed = engine.complete("exp-strategy-promotion-guard")

    assert completed.status is ExperimentStatus.COMPLETED
    assert completed.selected_candidate_id == "challenger"
    assert completed.previous_champion_id is None


def test_partial_benchmark_cannot_promote_or_persist_decision_state() -> None:
    engine, repository = _running_engine()
    engine.record(
        "exp-strategy-promotion-guard",
        MetricObservation("champion", "r1", "quality", 0.70),
    )

    with pytest.raises(ValueError, match="missing metric coverage"):
        engine.complete("exp-strategy-promotion-guard")

    recovered = repository.get("exp-strategy-promotion-guard")
    assert recovered.status is ExperimentStatus.RUNNING
    assert recovered.selected_candidate_id is None
    assert recovered.previous_champion_id is None


def test_failed_candidate_cannot_be_marked_promoted() -> None:
    engine, _repository = _running_engine()
    engine.record(
        "exp-strategy-promotion-guard",
        MetricObservation("champion", "r1", "quality", 0.80),
    )
    engine.record(
        "exp-strategy-promotion-guard",
        MetricObservation("challenger", "r1", "quality", 0.70),
    )

    completed = engine.complete("exp-strategy-promotion-guard")

    assert completed.status is ExperimentStatus.COMPLETED
    assert completed.selected_candidate_id == "champion"
    assert completed.previous_champion_id is None
