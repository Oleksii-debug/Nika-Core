import pytest

from nika_core.experiments import (
    ArtifactKind,
    ExperimentDefinition,
    ExperimentEngine,
    ExperimentStatus,
    InMemoryExperimentRepository,
    MetricObservation,
    MetricRule,
    PromotionPolicy,
    ReplayCase,
    StrategyRef,
)


def _strategy(candidate_id: str, permission: str = "perm-v1") -> StrategyRef:
    return StrategyRef(
        candidate_id=candidate_id,
        version="1",
        artifact_kind=ArtifactKind.PROMPT,
        artifact_ref=f"prompt://{candidate_id}/1",
        permission_fingerprint=permission,
    )


def _definition(*, threshold: float = 0.05, guardrail: float = 0.02) -> ExperimentDefinition:
    return ExperimentDefinition(
        experiment_id="exp-1",
        champion=_strategy("champion"),
        challengers=(_strategy("challenger-a"), _strategy("challenger-b")),
        replays=(
            ReplayCase("r1", "dataset://qa", "v1"),
            ReplayCase("r2", "dataset://qa", "v1"),
        ),
        policy=PromotionPolicy(
            primary_metric="quality",
            minimum_improvement=threshold,
            minimum_replays=2,
            guardrails=(MetricRule("safety", max_regression=guardrail),),
        ),
    )


def _record_pair(
    engine: ExperimentEngine,
    candidate: str,
    quality: tuple[float, float],
    safety: tuple[float, float],
) -> None:
    for replay_id, quality_value, safety_value in zip(("r1", "r2"), quality, safety, strict=True):
        engine.record("exp-1", MetricObservation(candidate, replay_id, "quality", quality_value))
        engine.record("exp-1", MetricObservation(candidate, replay_id, "safety", safety_value))


def test_candidate_cannot_change_permission_boundary() -> None:
    with pytest.raises(PermissionError):
        ExperimentDefinition(
            experiment_id="unsafe",
            champion=_strategy("champion", "perm-v1"),
            challengers=(_strategy("challenger", "perm-v2"),),
            replays=(ReplayCase("r1", "dataset://qa", "v1"),),
            policy=PromotionPolicy(primary_metric="quality"),
        )


def test_replay_coverage_and_duplicate_observations_fail_closed() -> None:
    repository = InMemoryExperimentRepository()
    engine = ExperimentEngine(repository)
    engine.create(_definition())
    engine.start("exp-1")
    observation = MetricObservation("champion", "r1", "quality", 0.8)
    engine.record("exp-1", observation)
    with pytest.raises(ValueError, match="duplicate"):
        engine.record("exp-1", observation)
    with pytest.raises(ValueError, match="insufficient replay coverage"):
        engine.complete("exp-1")


def test_best_eligible_challenger_is_promoted_deterministically() -> None:
    repository = InMemoryExperimentRepository()
    engine = ExperimentEngine(repository)
    engine.create(_definition())
    engine.start("exp-1")
    _record_pair(engine, "champion", (0.70, 0.70), (0.95, 0.95))
    _record_pair(engine, "challenger-a", (0.78, 0.78), (0.94, 0.94))
    _record_pair(engine, "challenger-b", (0.82, 0.82), (0.94, 0.94))

    completed = engine.complete("exp-1")
    assert completed.status is ExperimentStatus.PROMOTED
    assert completed.selected_candidate_id == "challenger-b"
    assert completed.previous_champion_id == "champion"


def test_guardrail_regression_denies_promotion_even_when_quality_improves() -> None:
    repository = InMemoryExperimentRepository()
    engine = ExperimentEngine(repository)
    engine.create(_definition())
    engine.start("exp-1")
    _record_pair(engine, "champion", (0.70, 0.70), (0.95, 0.95))
    _record_pair(engine, "challenger-a", (0.90, 0.90), (0.80, 0.80))
    _record_pair(engine, "challenger-b", (0.72, 0.72), (0.95, 0.95))

    completed = engine.complete("exp-1")
    assert completed.status is ExperimentStatus.COMPLETED
    assert completed.selected_candidate_id == "champion"


def test_promotion_can_roll_back_to_recorded_previous_champion() -> None:
    repository = InMemoryExperimentRepository()
    first_process = ExperimentEngine(repository)
    first_process.create(_definition())
    first_process.start("exp-1")
    _record_pair(first_process, "champion", (0.70, 0.70), (0.95, 0.95))
    _record_pair(first_process, "challenger-a", (0.80, 0.80), (0.95, 0.95))
    _record_pair(first_process, "challenger-b", (0.71, 0.71), (0.95, 0.95))
    promoted = first_process.complete("exp-1")
    assert promoted.selected_candidate_id == "challenger-a"

    recreated_engine = ExperimentEngine(repository)
    rolled_back = recreated_engine.rollback("exp-1")
    assert rolled_back.status is ExperimentStatus.ROLLED_BACK
    assert rolled_back.selected_candidate_id == "champion"


def test_unknown_replay_metric_and_candidate_are_rejected() -> None:
    repository = InMemoryExperimentRepository()
    engine = ExperimentEngine(repository)
    engine.create(_definition())
    engine.start("exp-1")
    with pytest.raises(ValueError, match="unknown candidate"):
        engine.record("exp-1", MetricObservation("ghost", "r1", "quality", 1.0))
    with pytest.raises(ValueError, match="unknown replay"):
        engine.record("exp-1", MetricObservation("champion", "r3", "quality", 1.0))
    with pytest.raises(ValueError, match="undeclared metric"):
        engine.record("exp-1", MetricObservation("champion", "r1", "cost", 1.0))
