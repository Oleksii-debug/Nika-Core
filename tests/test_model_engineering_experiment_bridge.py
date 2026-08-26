from __future__ import annotations

from dataclasses import replace

import pytest

from nika_core.experiments.contracts import MetricRule, PromotionPolicy
from nika_core.experiments.engine import ExperimentEngine
from nika_core.model_engineering import (
    COMPLETION_METRIC,
    LATENCY_METRIC,
    QUALITY_METRIC,
    TASK_PASS_METRIC,
    CandidateBenchmarkReport,
    CaseBenchmarkResult,
    EvaluationCase,
    EvaluationPurpose,
    EvaluationSet,
    ModelCandidate,
    benchmark_observations,
    build_experiment_definition,
)
from nika_core.model_gateway.contracts import ModelMessage, PrivacyClass, ProviderKind


def _candidate(candidate_id: str, model: str) -> ModelCandidate:
    return ModelCandidate(
        candidate_id=candidate_id,
        provider_id="ollama-local",
        provider_kind=ProviderKind.LOCAL,
        request_model=model,
        expected_response_model=model,
        engine_provenance_ref="engine:ollama-local",
        engine_license_ref="license:engine",
        model_provenance_ref=f"model:{model}",
        model_license_ref="license:model",
    )


def _evaluation(purpose: EvaluationPurpose = EvaluationPurpose.HELD_OUT) -> EvaluationSet:
    return EvaluationSet(
        evaluation_set_id="held-out-core",
        version="v3",
        provenance_ref="dataset:held-out-core",
        license_ref="license:eval",
        purpose=purpose,
        privacy=PrivacyClass.PUBLIC,
        cases=(
            EvaluationCase(
                case_id="r1",
                messages=(ModelMessage("user", "one"),),
                expected_text="one",
            ),
            EvaluationCase(
                case_id="r2",
                messages=(ModelMessage("user", "two"),),
                expected_text="two",
            ),
        ),
    )


def _report(
    candidate: ModelCandidate,
    evaluation: EvaluationSet,
    *,
    quality: tuple[float, float],
    latency: tuple[float, float],
) -> CandidateBenchmarkReport:
    results = tuple(
        CaseBenchmarkResult(
            candidate_id=candidate.candidate_id,
            case_id=case.case_id,
            score=score,
            passed=score >= case.pass_score,
            completion_succeeded=True,
            latency_ms=latency_ms,
            response_sha256="a" * 64,
            error_code=None,
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            resource_before=None,
            resource_after=None,
            accelerator_before=None,
            accelerator_after=None,
        )
        for case, score, latency_ms in zip(
            evaluation.cases,
            quality,
            latency,
            strict=True,
        )
    )
    return CandidateBenchmarkReport(
        candidate=candidate,
        evaluation_set_id=evaluation.evaluation_set_id,
        evaluation_set_version=evaluation.version,
        evaluation_set_sha256=evaluation.content_sha256,
        evaluation_purpose=evaluation.purpose,
        case_results=results,
        weighted_quality_score=sum(quality) / len(quality),
        task_pass_rate=sum(item.passed for item in results) / len(results),
        completion_rate=1.0,
        mean_latency_ms=sum(latency) / len(latency),
        p95_latency_ms=max(latency),
        peak_cpu_percent=None,
        peak_memory_percent=None,
        min_available_memory_bytes=None,
        peak_accelerator_percent=None,
        peak_accelerator_memory_bytes=None,
    )


class _MemoryExperimentRepository:
    def __init__(self) -> None:
        self.snapshot = None

    def create(self, snapshot) -> None:
        if self.snapshot is not None:
            raise ValueError("duplicate")
        self.snapshot = snapshot

    def get(self, experiment_id: str):
        if self.snapshot is None:
            raise KeyError(experiment_id)
        if self.snapshot.definition.experiment_id != experiment_id:
            raise KeyError(experiment_id)
        return self.snapshot

    def save(self, snapshot) -> None:
        self.snapshot = snapshot


def test_bridge_requires_held_out_evidence_for_promotion_definition() -> None:
    policy = PromotionPolicy(primary_metric=QUALITY_METRIC, minimum_replays=2)

    with pytest.raises(ValueError, match="held-out"):
        build_experiment_definition(
            experiment_id="model-promotion",
            champion=_candidate("champion", "m1"),
            challengers=(_candidate("challenger", "m2"),),
            evaluation_set=_evaluation(EvaluationPurpose.DEVELOPMENT),
            policy=policy,
            permission_fingerprint="permissions-v1",
        )


def test_bridge_uses_exact_evaluation_and_candidate_evidence() -> None:
    champion = _candidate("champion", "m1")
    challenger = _candidate("challenger", "m2")
    evaluation = _evaluation()
    policy = PromotionPolicy(
        primary_metric=QUALITY_METRIC,
        minimum_improvement=0.1,
        minimum_replays=2,
        guardrails=(
            MetricRule(
                metric=LATENCY_METRIC,
                higher_is_better=False,
                max_regression=20.0,
            ),
        ),
    )

    definition = build_experiment_definition(
        experiment_id="model-promotion",
        champion=champion,
        challengers=(challenger,),
        evaluation_set=evaluation,
        policy=policy,
        permission_fingerprint="permissions-v1",
    )

    assert definition.champion.version == f"sha256:{champion.evidence_sha256}"
    assert definition.challengers[0].artifact_ref.endswith(challenger.evidence_sha256)
    assert {item.replay_id for item in definition.replays} == {"r1", "r2"}
    for replay in definition.replays:
        assert evaluation.content_sha256 in replay.dataset_ref


def test_benchmark_observations_drive_existing_experiment_engine() -> None:
    champion = _candidate("champion", "m1")
    challenger = _candidate("challenger", "m2")
    evaluation = _evaluation()
    policy = PromotionPolicy(
        primary_metric=QUALITY_METRIC,
        minimum_improvement=0.1,
        minimum_replays=2,
        guardrails=(
            MetricRule(
                metric=LATENCY_METRIC,
                higher_is_better=False,
                max_regression=20.0,
            ),
        ),
    )
    definition = build_experiment_definition(
        experiment_id="model-promotion",
        champion=champion,
        challengers=(challenger,),
        evaluation_set=evaluation,
        policy=policy,
        permission_fingerprint="permissions-v1",
    )
    champion_report = _report(
        champion,
        evaluation,
        quality=(0.5, 0.5),
        latency=(100.0, 100.0),
    )
    challenger_report = _report(
        challenger,
        evaluation,
        quality=(1.0, 1.0),
        latency=(110.0, 110.0),
    )

    repository = _MemoryExperimentRepository()
    engine = ExperimentEngine(repository)
    engine.create(definition)
    engine.start(definition.experiment_id)
    metrics = (QUALITY_METRIC, LATENCY_METRIC)
    for report in (champion_report, challenger_report):
        for observation in benchmark_observations(report, metrics=metrics):
            engine.record(definition.experiment_id, observation)
    completed = engine.complete(definition.experiment_id)

    assert completed.selected_candidate_id == challenger.candidate_id
    assert completed.previous_champion_id == champion.candidate_id


def test_bridge_exposes_only_bounded_supported_metrics() -> None:
    report = _report(
        _candidate("candidate", "m"),
        _evaluation(),
        quality=(1.0, 0.0),
        latency=(10.0, 20.0),
    )

    observations = benchmark_observations(
        report,
        metrics=(
            QUALITY_METRIC,
            TASK_PASS_METRIC,
            COMPLETION_METRIC,
            LATENCY_METRIC,
        ),
    )
    assert len(observations) == 8
    assert {item.metric for item in observations} == {
        QUALITY_METRIC,
        TASK_PASS_METRIC,
        COMPLETION_METRIC,
        LATENCY_METRIC,
    }

    with pytest.raises(ValueError, match="unsupported benchmark metrics"):
        benchmark_observations(report, metrics=("invented_metric",))


def test_report_identity_guard_rejects_cross_candidate_rebinding() -> None:
    candidate = _candidate("candidate", "m")
    evaluation = _evaluation()
    report = _report(
        candidate,
        evaluation,
        quality=(1.0, 1.0),
        latency=(10.0, 10.0),
    )

    with pytest.raises(ValueError, match="candidate identity mismatch"):
        replace(
            report,
            candidate=_candidate("other", "m2"),
        )
