from __future__ import annotations

from collections import defaultdict
from hashlib import sha256
import json
from statistics import fmean

from nika_core.experiments.contracts import (
    ArtifactKind,
    ExperimentDefinition,
    MetricObservation,
    PromotionPolicy,
    ReplayCase,
    StrategyRef,
)

from nika_core.model_lab.contracts import (
    AttemptStatus,
    BenchmarkRunEvidence,
    BenchmarkSuite,
    ModelCandidate,
)


def candidate_identity_sha256(candidate: ModelCandidate) -> str:
    payload = {
        "candidate_id": candidate.candidate_id,
        "provider_id": candidate.provider_id,
        "provider_kind": candidate.provider_kind.value,
        "model": candidate.model,
        "model_version": candidate.model_version,
        "license_reference": candidate.license_reference,
        "provenance_reference": candidate.provenance_reference,
        "permission_fingerprint": candidate.permission_fingerprint,
        "artifact_sha256": (
            candidate.artifact_sha256.lower()
            if candidate.artifact_sha256 is not None
            else None
        ),
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(canonical).hexdigest()


def candidate_strategy_ref(candidate: ModelCandidate) -> StrategyRef:
    artifact_ref = (
        f"model-lab://{candidate.candidate_id}@{candidate.model_version}"
        f"#sha256={candidate_identity_sha256(candidate)}"
    )
    return StrategyRef(
        candidate_id=candidate.candidate_id,
        version=candidate.model_version,
        artifact_kind=ArtifactKind.CONFIG,
        artifact_ref=artifact_ref,
        permission_fingerprint=candidate.permission_fingerprint,
    )


def suite_replays(suite: BenchmarkSuite) -> tuple[ReplayCase, ...]:
    return tuple(
        ReplayCase(
            replay_id=case.case_id,
            dataset_ref=case.dataset_ref,
            dataset_version=case.dataset_version,
        )
        for case in suite.cases
    )


def build_experiment_definition(
    *,
    experiment_id: str,
    champion: ModelCandidate,
    challengers: tuple[ModelCandidate, ...],
    suite: BenchmarkSuite,
    policy: PromotionPolicy,
) -> ExperimentDefinition:
    return ExperimentDefinition(
        experiment_id=experiment_id,
        champion=candidate_strategy_ref(champion),
        challengers=tuple(candidate_strategy_ref(candidate) for candidate in challengers),
        replays=suite_replays(suite),
        policy=policy,
    )


def metric_observations(
    evidence: BenchmarkRunEvidence,
    *,
    metrics: tuple[str, ...],
) -> tuple[MetricObservation, ...]:
    if not evidence.complete:
        raise ValueError("incomplete benchmark evidence cannot enter Experiment Engine")
    if not metrics:
        raise ValueError("at least one metric must be requested")
    if len(metrics) != len(set(metrics)):
        raise ValueError("requested experiment metrics must be unique")

    by_case: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    repetitions_by_case: dict[str, int] = defaultdict(int)

    for attempt in evidence.attempts:
        if attempt.status is not AttemptStatus.SUCCESS:
            raise ValueError("failed benchmark attempt cannot enter Experiment Engine")
        repetitions_by_case[attempt.case_id] += 1
        attempt_metrics = {metric.metric: float(metric.value) for metric in attempt.metrics}
        for metric_name in metrics:
            if metric_name not in attempt_metrics:
                raise ValueError(
                    f"benchmark evidence is missing metric {metric_name!r} "
                    f"for case {attempt.case_id!r}"
                )
            by_case[attempt.case_id][metric_name].append(attempt_metrics[metric_name])

    observations: list[MetricObservation] = []
    for case_id in sorted(by_case):
        repetitions = repetitions_by_case[case_id]
        for metric_name in metrics:
            values = by_case[case_id][metric_name]
            if len(values) != repetitions:
                raise ValueError(
                    f"incomplete repetition coverage for {case_id}/{metric_name}: "
                    f"expected {repetitions}, got {len(values)}"
                )
            observations.append(
                MetricObservation(
                    candidate_id=evidence.candidate.candidate_id,
                    replay_id=case_id,
                    metric=metric_name,
                    value=fmean(values),
                )
            )
    return tuple(observations)
