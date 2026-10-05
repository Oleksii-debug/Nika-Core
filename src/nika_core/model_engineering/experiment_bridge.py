from __future__ import annotations

from collections.abc import Sequence
from math import ceil
from statistics import fmean

from nika_core.experiments.contracts import (
    ArtifactKind,
    ExperimentDefinition,
    MetricObservation,
    PromotionPolicy,
    ReplayCase,
    StrategyRef,
)
from nika_core.model_engineering.contracts import (
    BenchmarkExecutionConfig,
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
    execution_config: BenchmarkExecutionConfig,
    policy: PromotionPolicy,
    permission_fingerprint: str,
) -> ExperimentDefinition:
    """Build an Experiment Engine definition without creating or promoting it."""

    if type(experiment_id) is not str:
        raise TypeError("experiment_id must be canonical text")
    if not experiment_id or experiment_id != experiment_id.strip():
        raise ValueError("experiment_id must be non-empty without surrounding whitespace")
    if type(champion) is not ModelCandidate:
        raise TypeError("champion must be an exact ModelCandidate")
    if type(challengers) is not tuple:
        raise TypeError("challengers must be a canonical tuple")
    if any(type(candidate) is not ModelCandidate for candidate in challengers):
        raise TypeError("challengers must use exact ModelCandidate values")
    if type(evaluation_set) is not EvaluationSet:
        raise TypeError("evaluation_set must be an exact EvaluationSet")
    if type(execution_config) is not BenchmarkExecutionConfig:
        raise TypeError("execution_config must be an exact BenchmarkExecutionConfig")
    if type(policy) is not PromotionPolicy:
        raise TypeError("policy must be an exact PromotionPolicy")
    if type(permission_fingerprint) is not str:
        raise TypeError("permission_fingerprint must be canonical text")
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
        champion=_strategy_ref(
            champion,
            permission_fingerprint,
            execution_config.evidence_sha256,
        ),
        challengers=tuple(
            _strategy_ref(
                candidate,
                permission_fingerprint,
                execution_config.evidence_sha256,
            )
            for candidate in challengers
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

    if type(report) is not CandidateBenchmarkReport:
        raise TypeError("report must be an exact CandidateBenchmarkReport")
    if type(definition) is not ExperimentDefinition:
        raise TypeError("definition must be an exact ExperimentDefinition")
    if type(evaluation_set) is not EvaluationSet:
        raise TypeError("evaluation_set must be an exact EvaluationSet")
    if evaluation_set.purpose is not EvaluationPurpose.HELD_OUT:
        raise ValueError("model promotion observations require held-out evidence")
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
    for case, result in zip(
        evaluation_set.cases,
        report.case_results,
        strict=True,
    ):
        expected_pass = (
            result.completion_succeeded
            and result.score >= float(case.pass_score)
        )
        if result.passed is not expected_pass:
            raise ValueError(
                "benchmark case pass evidence does not match the evaluation threshold"
            )
    _validate_report_aggregates(report, evaluation_set)

    candidate_refs = (definition.champion, *definition.challengers)
    matching_refs = tuple(
        candidate_ref
        for candidate_ref in candidate_refs
        if candidate_ref.candidate_id == report.candidate.candidate_id
    )
    if len(matching_refs) != 1:
        raise ValueError("benchmark candidate is not uniquely declared by the experiment")
    candidate_ref = matching_refs[0]
    expected_ref = _strategy_ref(
        report.candidate,
        candidate_ref.permission_fingerprint,
        report.execution_config_sha256,
    )
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
            if metric != LATENCY_METRIC or result.completion_succeeded
        )
    return tuple(observations)


def _validate_report_aggregates(
    report: CandidateBenchmarkReport,
    evaluation_set: EvaluationSet,
) -> None:
    total_weight = sum(float(case.weight) for case in evaluation_set.cases)
    expected_quality = sum(
        result.score * float(case.weight)
        for case, result in zip(
            evaluation_set.cases,
            report.case_results,
            strict=True,
        )
    ) / total_weight
    expected_pass_rate = (
        sum(result.passed for result in report.case_results)
        / len(report.case_results)
    )
    expected_completion_rate = (
        sum(result.completion_succeeded for result in report.case_results)
        / len(report.case_results)
    )
    successful_latencies = [
        result.latency_ms
        for result in report.case_results
        if result.completion_succeeded
    ]
    expected_mean_latency = (
        fmean(successful_latencies)
        if successful_latencies
        else None
    )
    expected_p95_latency = None
    if successful_latencies:
        ordered = sorted(float(value) for value in successful_latencies)
        index = max(0, ceil(0.95 * len(ordered)) - 1)
        expected_p95_latency = ordered[index]

    if (
        report.weighted_quality_score != expected_quality
        or report.task_pass_rate != expected_pass_rate
        or report.completion_rate != expected_completion_rate
        or report.mean_latency_ms != expected_mean_latency
        or report.p95_latency_ms != expected_p95_latency
    ):
        raise ValueError("benchmark report aggregate metrics do not match case evidence")


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


def _strategy_ref(
    candidate: ModelCandidate,
    permission_fingerprint: str,
    execution_config_sha256: str,
) -> StrategyRef:
    return StrategyRef(
        candidate_id=candidate.candidate_id,
        version=f"sha256:{candidate.evidence_sha256}",
        artifact_kind=ArtifactKind.CONFIG,
        artifact_ref=(
            f"model-candidate:sha256:{candidate.evidence_sha256}:"
            f"benchmark-config:sha256:{execution_config_sha256}"
        ),
        permission_fingerprint=permission_fingerprint,
    )
