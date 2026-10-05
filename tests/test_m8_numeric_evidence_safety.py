"""M8 promotion must reject invalid policy evidence and nonfinite calculations."""

import json

import pytest

from nika_core.experiments import (
    ArtifactKind, ExperimentDefinition, ExperimentEngine, ExperimentStatus,
    InMemoryExperimentRepository, MetricObservation, MetricRule, PromotionPolicy,
    ReplayCase, StrategyRef,
)
from nika_core.experiments.repository import _decode_definition, _encode_definition


def _definition(*, guardrails=(), replays=("r1",)):
    champion = StrategyRef("champion", "1", ArtifactKind.PROMPT, "prompt://c", "perm")
    challenger = StrategyRef("challenger", "1", ArtifactKind.PROMPT, "prompt://x", "perm")
    return ExperimentDefinition(
        "numeric-safety", champion, (challenger,),
        tuple(ReplayCase(r, "dataset://fixed", "v1") for r in replays),
        PromotionPolicy("quality", guardrails=guardrails, minimum_replays=len(replays)),
    )


@pytest.mark.parametrize("bad", [True, False, "0.5", None, float("nan"), float("inf"), 10**400])
def test_observation_rejects_non_numeric_or_unrepresentable_evidence(bad):
    with pytest.raises(ValueError, match="finite"):
        MetricObservation("champion", "r1", "quality", bad)


@pytest.mark.parametrize("bad", [True, "0.5", float("nan"), float("-inf"), 10**400])
def test_policy_thresholds_reject_ambiguous_numbers(bad):
    with pytest.raises(ValueError):
        PromotionPolicy("quality", minimum_improvement=bad)
    with pytest.raises(ValueError):
        MetricRule("safety", max_regression=bad)


@pytest.mark.parametrize("bad", [True, 1.0, "1", 0, -1])
def test_minimum_replays_requires_a_positive_integer(bad):
    with pytest.raises(ValueError, match="positive integer"):
        PromotionPolicy("quality", minimum_replays=bad)


def test_policy_directions_must_be_actual_booleans():
    with pytest.raises(ValueError, match="boolean"):
        PromotionPolicy("quality", primary_higher_is_better="false")
    with pytest.raises(ValueError, match="boolean"):
        MetricRule("safety", higher_is_better=0)


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("minimum_improvement", "0.1"),
        ("minimum_replays", True),
        ("minimum_replays", 1.5),
        ("primary_higher_is_better", "false"),
    ],
)
def test_reloaded_policy_rejects_coercion(field, bad):
    data = json.loads(_encode_definition(_definition()))
    data["policy"][field] = bad
    with pytest.raises(ValueError):
        _decode_definition(json.dumps(data))


def test_nonfinite_persisted_definition_is_rejected():
    raw = _encode_definition(_definition()).replace(
        '"minimum_improvement":0.0', '"minimum_improvement":NaN'
    )
    with pytest.raises(ValueError, match="nonfinite JSON"):
        _decode_definition(raw)


def _running(definition):
    repo = InMemoryExperimentRepository()
    engine = ExperimentEngine(repo)
    engine.create(definition)
    engine.start(definition.experiment_id)
    return engine, repo


def test_primary_improvement_overflow_cannot_promote():
    engine, repo = _running(_definition())
    engine.record("numeric-safety", MetricObservation("champion", "r1", "quality", -1e308))
    engine.record("numeric-safety", MetricObservation("challenger", "r1", "quality", 1e308))
    with pytest.raises(ValueError, match="improvement is not finite"):
        engine.complete("numeric-safety")
    assert repo.get("numeric-safety").status is ExperimentStatus.RUNNING


def test_finite_values_with_overflowed_mean_cannot_promote():
    engine, repo = _running(_definition(replays=("r1", "r2")))
    for candidate in ("champion", "challenger"):
        for replay in ("r1", "r2"):
            observation = MetricObservation(candidate, replay, "quality", 1e308)
            engine.record("numeric-safety", observation)
    with pytest.raises(ValueError, match="mean exceeds finite range"):
        engine.complete("numeric-safety")
    assert repo.get("numeric-safety").status is ExperimentStatus.RUNNING


def test_guardrail_regression_overflow_cannot_promote():
    engine, repo = _running(_definition(guardrails=(MetricRule("safety"),)))
    for candidate, quality, safety in (
        ("champion", 0.0, 1e308), ("challenger", 1.0, -1e308)
    ):
        engine.record(
            "numeric-safety", MetricObservation(candidate, "r1", "quality", quality)
        )
        engine.record(
            "numeric-safety", MetricObservation(candidate, "r1", "safety", safety)
        )
    with pytest.raises(ValueError, match="regression is not finite"):
        engine.complete("numeric-safety")
    assert repo.get("numeric-safety").status is ExperimentStatus.RUNNING
