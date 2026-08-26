from __future__ import annotations

from collections.abc import Sequence

from nika_core.experiments.contracts import (
    ArtifactKind,
    ExperimentDefinition,
    MetricObservation,
    PromotionPolicy,
    ReplayCase,
    StrategyRef,
)
from nika_core.model_engineering.contracts import (
    CandidateBenchmarkReport,
    EvaluationPurpose,
    EvaluationSet,
    ModelCandidate,
)

QUALITY_METRIC = "model_quality_score"
TASK_PASS_METRIC = "model_task_pass"
COMPLETION_METRIC = "model_completion_success"
LATENCY_METRIC = "model_latency_ms"
_SUPPORTED_METRICS = frozenset(
    {
        QUALITY_METRIC,
        TASK_PASS_METRIC,
        COMPLETION_METRIC,
        LATENCY_METRIC,
    }
)


def build_experiment_definition(
    *,
    experiment_id: str,
    champion: ModelCandidate,
    challengers: Sequence[ModelCandidate],
    evaluation_set: EvaluationSet,
    policy: PromotionPolicy,
    permission_fingerprint: str,
) -> ExperimentDefinition:
    """Build an Experiment Engine definition without creating or promoting it."""

    if evaluation_set.purpose is not EvaluationPurpose.HELD_OUT:
        raise ValueError("model promotion experiments require a held-out evaluation set")
    if not challengers:
        raise ValueError("at least one challenger is required")
    candidates = (champion, *challengers)
    ids = [candidate.candidate_id for candidate in candidates]
    if len(ids) != len(set(ids)):
        raise ValueError("model promotion candidate IDs must be unique")
    if not permission_fingerprint or permission_fingerprint != permission_fingerprint.strip():
        raise ValueError("permission_fingerprint must be non-empty without surrounding whitespace")
    policy_metrics = {
        policy.primary_metric,
        *(rule.metric for rule in policy.guardrails),
    }
    unsupported = policy_metrics - _SUPPORTED_METRICS
    if unsupported:
        raise ValueError(f"unsupported model promotion metrics: {sorted(unsupported)}")

    return ExperimentDefinition(
        experiment_id=experiment_id,
        champion=_strategy_ref(champion, permission_fingerprint),
        challengers=tuple(
            _strategy_ref(candidate, permission_fingerprint) for candidate in challengers
        ),
        replays=tuple(
            ReplayCase(
                replay_id=case.case_id,
                dataset_ref=(
                    f"model-evaluation:{evaluation_set.evaluation_set_id}:"
                    f"sha256:{evaluation_set.content_sha256}"
                ),
                dataset_version=evaluation_set.version,
            )
            for case in evaluation_set.cases
        ),
        policy=policy,
    )


def benchmark_observations(
    report: CandidateBenchmarkReport,
    *,
    metrics: Sequence[str],
) -> tuple[MetricObservation, ...]:
    """Project benchmark case evidence into the existing Experiment Engine vocabulary."""

    if not metrics:
        raise ValueError("at least one benchmark metric is required")
    if len(metrics) != len(set(metrics)):
        raise ValueError("benchmark metrics must be unique")
    unknown = set(metrics) - _SUPPORTED_METRICS
    if unknown:
        raise ValueError(f"unsupported benchmark metrics: {sorted(unknown)}")

    observations: list[MetricObservation] = []
    for result in report.case_results:
        values = {
            QUALITY_METRIC: result.score,
            TASK_PASS_METRIC: float(result.passed),
            COMPLETION_METRIC: float(result.completion_succeeded),
            LATENCY_METRIC: result.latency_ms,
        }
        observations.extend(
            MetricObservation(
                candidate_id=report.candidate.candidate_id,
                replay_id=result.case_id,
                metric=metric,
                value=values[metric],
            )
            for metric in metrics
        )
    return tuple(observations)


def _strategy_ref(candidate: ModelCandidate, permission_fingerprint: str) -> StrategyRef:
    return StrategyRef(
        candidate_id=candidate.candidate_id,
        version=f"sha256:{candidate.evidence_sha256}",
        artifact_kind=ArtifactKind.CONFIG,
        artifact_ref=f"model-candidate:sha256:{candidate.evidence_sha256}",
        permission_fingerprint=permission_fingerprint,
    )
