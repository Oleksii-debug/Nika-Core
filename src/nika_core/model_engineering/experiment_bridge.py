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
    _validate_policy_metrics(policy)

    return ExperimentDefinition(
        experiment_id=experiment_id,
        champion=_strategy_ref(champion, permission_fingerprint),
        challengers=tuple(
            _strategy_ref(candidate, permission_fingerprint) for candidate in challengers
        ),
        replays=_evaluation_replays(evaluation_set),
        policy=policy,
    )


def benchmark_observations(
    report: CandidateBenchmarkReport,
    *,
    definition: ExperimentDefinition,
    evaluation_set: EvaluationSet,
) -> tuple[MetricObservation, ...]:
    """Project exact benchmark evidence into its exact Experiment Engine definition."""

    metrics = _validate_policy_metrics(definition.policy)
    expected_replays = _evaluation_replays(evaluation_set)
    if definition.replays != expected_replays:
        raise ValueError("experiment replay evidence does not match the supplied evaluation set")
    if (
        report.evaluation_set_id != evaluation_set.evaluation_set_id
        or report.evaluation_set_version != evaluation_set.version
        or report.evaluation_set_sha256 != evaluation_set.content_sha256
        or report.evaluation_purpose is not evaluation_set.purpose
    ):
        raise ValueError("benchmark report does not match the supplied evaluation set evidence")
    expected_case_ids = tuple(case.case_id for case in evaluation_set.cases)
    actual_case_ids = tuple(result.case_id for result in report.case_results)
    if actual_case_ids != expected_case_ids:
        raise ValueError("benchmark report case coverage/order does not match the evaluation set")

    candidate_refs = (definition.champion, *definition.challengers)
    matching_refs = tuple(
        candidate_ref
        for candidate_ref in candidate_refs
        if candidate_ref.candidate_id == report.candidate.candidate_id
    )
    if len(matching_refs) != 1:
        raise ValueError("benchmark candidate is not uniquely declared by the experiment")
    candidate_ref = matching_refs[0]
    expected_ref = _strategy_ref(report.candidate, candidate_ref.permission_fingerprint)
    if candidate_ref != expected_ref:
        raise ValueError("benchmark candidate evidence does not match the experiment strategy ref")

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


def _validate_policy_metrics(policy: PromotionPolicy) -> tuple[str, ...]:
    metrics = (
        policy.primary_metric,
        *(rule.metric for rule in policy.guardrails),
    )
    unsupported = set(metrics) - _SUPPORTED_METRICS
    if unsupported:
        raise ValueError(f"unsupported model promotion metrics: {sorted(unsupported)}")
    return metrics


def _evaluation_replays(evaluation_set: EvaluationSet) -> tuple[ReplayCase, ...]:
    return tuple(
        ReplayCase(
            replay_id=case.case_id,
            dataset_ref=(
                f"model-evaluation:{evaluation_set.evaluation_set_id}:"
                f"sha256:{evaluation_set.content_sha256}"
            ),
            dataset_version=evaluation_set.version,
        )
        for case in evaluation_set.cases
    )


def _strategy_ref(candidate: ModelCandidate, permission_fingerprint: str) -> StrategyRef:
    return StrategyRef(
        candidate_id=candidate.candidate_id,
        version=f"sha256:{candidate.evidence_sha256}",
        artifact_kind=ArtifactKind.CONFIG,
        artifact_ref=f"model-candidate:sha256:{candidate.evidence_sha256}",
        permission_fingerprint=permission_fingerprint,
    )
