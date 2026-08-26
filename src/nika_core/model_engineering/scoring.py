from __future__ import annotations

import hashlib
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal, ROUND_HALF_EVEN

from nika_core.model_engineering.contracts import (
    BenchmarkMetric,
    BenchmarkObservation,
    BenchmarkRecommendation,
    BenchmarkSuite,
    CandidateExclusion,
    CandidateScore,
    ModelCandidate,
    canonical_json,
)

_MICROS = Decimal(1_000_000)


class BenchmarkEvidenceError(ValueError):
    """Raised when benchmark evidence is inconsistent with the declared suite."""


def _normalized_metric_score(metric: BenchmarkMetric, value: float) -> Decimal:
    observed = Decimal(str(value))
    worst = Decimal(str(metric.worst_value))
    best = Decimal(str(metric.best_value))
    normalized = (observed - worst) / (best - worst)
    return min(Decimal(1), max(Decimal(0), normalized))


def _to_micros(value: Decimal) -> int:
    return int((value * _MICROS).quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def rank_benchmark_candidates(
    suite: BenchmarkSuite,
    observations: tuple[BenchmarkObservation, ...],
    *,
    created_at: datetime | None = None,
) -> BenchmarkRecommendation:
    if not observations:
        raise BenchmarkEvidenceError("at least one observation is required")

    run_ids = {observation.run_id for observation in observations}
    if len(run_ids) != 1:
        raise BenchmarkEvidenceError("all observations must belong to one run")
    run_id = next(iter(run_ids))

    metric_names = tuple(metric.name for metric in suite.metrics)
    expected_metric_names = set(metric_names)
    required_cases = set(suite.required_case_ids)

    grouped: dict[str, list[BenchmarkObservation]] = defaultdict(list)
    candidates: dict[str, ModelCandidate] = {}
    source_digests: list[str] = []

    for observation in observations:
        if observation.suite_key != suite.key:
            raise BenchmarkEvidenceError(
                "observation suite identity does not match benchmark suite"
            )
        if observation.case_id not in required_cases:
            raise BenchmarkEvidenceError(f"unexpected benchmark case: {observation.case_id}")
        observed_metric_names = {metric.name for metric in observation.metrics}
        if observed_metric_names != expected_metric_names:
            raise BenchmarkEvidenceError(
                f"metric set mismatch for observation {observation.observation_id}"
            )
        key = observation.candidate.key
        candidates[key] = observation.candidate
        grouped[key].append(observation)
        source_digests.append(
            hashlib.sha256(canonical_json(observation.to_payload()).encode("utf-8")).hexdigest()
        )

    ranked: list[CandidateScore] = []
    excluded: list[CandidateExclusion] = []
    total_weight = sum(Decimal(str(metric.weight)) for metric in suite.metrics)

    for candidate_key in sorted(candidates):
        candidate = candidates[candidate_key]
        candidate_observations = grouped[candidate_key]
        by_case: dict[str, BenchmarkObservation] = {}
        for observation in candidate_observations:
            if observation.case_id in by_case:
                raise BenchmarkEvidenceError(
                    f"duplicate case evidence for candidate {candidate_key}: {observation.case_id}"
                )
            by_case[observation.case_id] = observation

        missing = sorted(required_cases - set(by_case))
        if missing:
            excluded.append(
                CandidateExclusion(
                    candidate=candidate,
                    reason="missing_required_cases:" + ",".join(missing),
                )
            )
            continue

        metric_scores: list[tuple[str, int]] = []
        weighted_total = Decimal(0)
        for metric in suite.metrics:
            normalized_values: list[Decimal] = []
            for case_id in suite.required_case_ids:
                observation = by_case[case_id]
                values = {item.name: item.value for item in observation.metrics}
                normalized_values.append(_normalized_metric_score(metric, values[metric.name]))
            mean_score = sum(normalized_values, Decimal(0)) / Decimal(len(normalized_values))
            metric_scores.append((metric.name, _to_micros(mean_score)))
            weighted_total += mean_score * Decimal(str(metric.weight))

        overall = weighted_total / total_weight
        ranked.append(
            CandidateScore(
                candidate=candidate,
                score_micros=_to_micros(overall),
                metric_score_micros=tuple(metric_scores),
            )
        )

    if not ranked:
        raise BenchmarkEvidenceError("no candidate has complete required benchmark evidence")

    ranked.sort(key=lambda item: (-item.score_micros, item.candidate.key))
    excluded.sort(key=lambda item: item.candidate.key)
    evidence_payload = {
        "excluded_candidates": [item.to_payload() for item in excluded],
        "ranked_candidates": [item.to_payload() for item in ranked],
        "run_id": run_id,
        "source_observation_sha256": sorted(source_digests),
        "suite_sha256": hashlib.sha256(
            canonical_json(suite.to_payload()).encode("utf-8")
        ).hexdigest(),
    }
    evidence_sha256 = hashlib.sha256(
        canonical_json(evidence_payload).encode("utf-8")
    ).hexdigest()
    recommendation_id = f"mel-{evidence_sha256[:24]}"
    recommendation_time = created_at or datetime.now(UTC)

    return BenchmarkRecommendation(
        recommendation_id=recommendation_id,
        run_id=run_id,
        suite_id=suite.suite_id,
        suite_version=suite.version,
        ranked_candidates=tuple(ranked),
        excluded_candidates=tuple(excluded),
        source_observation_sha256=tuple(sorted(source_digests)),
        evidence_sha256=evidence_sha256,
        created_at=recommendation_time,
    )
