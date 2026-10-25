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
    BenchmarkExecutionConfig,
    BenchmarkRunEvidence,
    CandidateBenchmarkReport,
    CaseBenchmarkResult,
    EvaluationCase,
    EvaluationPurpose,
    EvaluationSet,
    ModelCandidate,
    benchmark_observations,
    build_experiment_definition,
)
from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelMessage,
    PrivacyClass,
    ProviderKind,
)


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


def _execution_config(
    timeout_seconds: float = 60.0,
    temperature: float | None = 0.0,
) -> BenchmarkExecutionConfig:
    return BenchmarkExecutionConfig(
        timeout_seconds=timeout_seconds,
        temperature=temperature,
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
    execution_config: BenchmarkExecutionConfig | None = None,
) -> CandidateBenchmarkReport:
    results = tuple(
        CaseBenchmarkResult(
            candidate_id=candidate.candidate_id,
            case_id=case.case_id,
            evaluation_weight=float(case.weight),
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
    config = _execution_config() if execution_config is None else execution_config
    return CandidateBenchmarkReport(
        candidate=candidate,
        run=BenchmarkRunEvidence(
            run_id="bridge-fixture-run",
            configuration_sha256=benchmark_configuration_sha256(
                candidate_evidence_sha256=candidate.evidence_sha256,
                evaluation_set_id=evaluation.evaluation_set_id,
                evaluation_set_version=evaluation.version,
                evaluation_set_sha256=evaluation.content_sha256,
                execution_config_sha256=config.evidence_sha256,
            ),
        ),
        evaluation_set_id=evaluation.evaluation_set_id,
        evaluation_set_version=evaluation.version,
        evaluation_set_sha256=evaluation.content_sha256,
        execution_config_sha256=config.evidence_sha256,
        evaluation_purpose=evaluation.purpose,
        case_results=results,
        weighted_quality_score=(
            sum(
                result.score * float(result.evaluation_weight)
                for result in results
            )
            / sum(float(result.evaluation_weight) for result in results)
        ),
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
            execution_config=_execution_config(),
            policy=policy,
            permission_fingerprint="permissions-v1",
        )


def test_bridge_rejects_policy_metrics_it_cannot_supply() -> None:
    policy = PromotionPolicy(
        primary_metric=QUALITY_METRIC,
        minimum_replays=2,
        guardrails=(MetricRule(metric="invented_metric"),),
    )

    with pytest.raises(ValueError, match="unsupported model promotion metrics"):
        build_experiment_definition(
            experiment_id="model-promotion",
            champion=_candidate("champion", "m1"),
            challengers=(_candidate("challenger", "m2"),),
            evaluation_set=_evaluation(),
            execution_config=_execution_config(),
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
        execution_config=_execution_config(),
        policy=policy,
        permission_fingerprint="permissions-v1",
    )

    assert definition.champion.version == f"sha256:{champion.evidence_sha256}"
    assert challenger.evidence_sha256 in definition.challengers[0].artifact_ref
    assert _execution_config().evidence_sha256 in definition.challengers[0].artifact_ref
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
        execution_config=_execution_config(),
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
    for report in (champion_report, challenger_report):
        for observation in benchmark_observations(
            report,
            definition=definition,
            evaluation_set=evaluation,
        ):
            engine.record(definition.experiment_id, observation)
    completed = engine.complete(definition.experiment_id)

    assert completed.selected_candidate_id == challenger.candidate_id
    assert completed.previous_champion_id == champion.candidate_id


def test_bridge_projects_only_metrics_declared_by_the_exact_policy() -> None:
    candidate = _candidate("candidate", "m")
    evaluation = _evaluation()
    report = _report(
        candidate,
        evaluation,
        quality=(1.0, 0.0),
        latency=(10.0, 20.0),
    )
    definition = build_experiment_definition(
        experiment_id="all-metrics",
        champion=candidate,
        challengers=(_candidate("other", "m2"),),
        evaluation_set=evaluation,
        execution_config=_execution_config(),
        policy=PromotionPolicy(
            primary_metric=QUALITY_METRIC,
            minimum_replays=2,
            guardrails=(
                MetricRule(metric=TASK_PASS_METRIC),
                MetricRule(metric=COMPLETION_METRIC),
                MetricRule(metric=LATENCY_METRIC, higher_is_better=False),
            ),
        ),
        permission_fingerprint="permissions-v1",
    )

    observations = benchmark_observations(
        report,
        definition=definition,
        evaluation_set=evaluation,
    )
    assert len(observations) == 8
    assert {item.metric for item in observations} == {
        QUALITY_METRIC,
        TASK_PASS_METRIC,
        COMPLETION_METRIC,
        LATENCY_METRIC,
    }


def test_bridge_rejects_cross_evaluation_evidence_rebinding() -> None:
    candidate = _candidate("candidate", "m")
    source_evaluation = _evaluation()
    report = _report(
        candidate,
        source_evaluation,
        quality=(1.0, 1.0),
        latency=(10.0, 10.0),
    )
    target_evaluation = replace(source_evaluation, version="v4")
    definition = build_experiment_definition(
        experiment_id="cross-eval",
        champion=candidate,
        challengers=(_candidate("other", "m2"),),
        evaluation_set=target_evaluation,
        execution_config=_execution_config(),
        policy=PromotionPolicy(primary_metric=QUALITY_METRIC, minimum_replays=2),
        permission_fingerprint="permissions-v1",
    )

    with pytest.raises(ValueError, match="does not match the supplied evaluation set"):
        benchmark_observations(
            report,
            definition=definition,
            evaluation_set=target_evaluation,
        )


def test_bridge_rejects_same_candidate_id_with_different_model_evidence() -> None:
    evaluation = _evaluation()
    trusted_candidate = _candidate("candidate", "m1")
    definition = build_experiment_definition(
        experiment_id="candidate-substitution",
        champion=trusted_candidate,
        challengers=(_candidate("other", "m3"),),
        evaluation_set=evaluation,
        execution_config=_execution_config(),
        policy=PromotionPolicy(primary_metric=QUALITY_METRIC, minimum_replays=2),
        permission_fingerprint="permissions-v1",
    )
    substituted_report = _report(
        _candidate("candidate", "m2"),
        evaluation,
        quality=(1.0, 1.0),
        latency=(10.0, 10.0),
    )

    with pytest.raises(ValueError, match="candidate evidence"):
        benchmark_observations(
            substituted_report,
            definition=definition,
            evaluation_set=evaluation,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("weighted_quality_score", 0.25),
        ("task_pass_rate", 0.25),
        ("completion_rate", 0.25),
        ("mean_latency_ms", 999.0),
        ("p95_latency_ms", 999.0),
    ],
)
def test_observation_bridge_rejects_incoherent_report_aggregates(
    field: str,
    value: float,
) -> None:
    candidate = _candidate("candidate", "m")
    evaluation = _evaluation()
    report = _report(
        candidate,
        evaluation,
        quality=(1.0, 1.0),
        latency=(10.0, 20.0),
    )
    forged = replace(report, **{field: value})
    definition = build_experiment_definition(
        experiment_id="aggregate-binding",
        champion=candidate,
        challengers=(_candidate("other", "m2"),),
        evaluation_set=evaluation,
        execution_config=_execution_config(),
        policy=PromotionPolicy(
            primary_metric=QUALITY_METRIC,
            minimum_replays=2,
        ),
        permission_fingerprint="permissions-v1",
    )

    with pytest.raises(ValueError, match="aggregate metrics"):
        benchmark_observations(
            forged,
            definition=definition,
            evaluation_set=evaluation,
        )


def test_observation_bridge_rejects_case_weight_substitution() -> None:
    candidate = _candidate("candidate", "m")
    base_evaluation = _evaluation()
    evaluation = replace(
        base_evaluation,
        cases=(
            replace(base_evaluation.cases[0], weight=1.0),
            replace(base_evaluation.cases[1], weight=3.0),
        ),
    )
    report = _report(
        candidate,
        evaluation,
        quality=(1.0, 0.0),
        latency=(10.0, 20.0),
    )
    forged_second = replace(report.case_results[1], evaluation_weight=1.0)
    forged = replace(
        report,
        case_results=(report.case_results[0], forged_second),
        weighted_quality_score=0.5,
    )
    definition = build_experiment_definition(
        experiment_id="weight-binding",
        champion=candidate,
        challengers=(_candidate("other", "m2"),),
        evaluation_set=evaluation,
        execution_config=_execution_config(),
        policy=PromotionPolicy(primary_metric=QUALITY_METRIC, minimum_replays=2),
        permission_fingerprint="permissions-v1",
    )

    with pytest.raises(ValueError, match="weight evidence"):
        benchmark_observations(
            forged,
            definition=definition,
            evaluation_set=evaluation,
        )


def test_observation_bridge_rejects_pass_flag_that_conflicts_with_threshold() -> None:
    candidate = _candidate("candidate", "m")
    evaluation = _evaluation()
    report = _report(
        candidate,
        evaluation,
        quality=(1.0, 0.0),
        latency=(10.0, 20.0),
    )
    first = replace(report.case_results[0], passed=False)
    forged = replace(report, case_results=(first, report.case_results[1]))
    definition = build_experiment_definition(
        experiment_id="threshold-binding",
        champion=candidate,
        challengers=(_candidate("other", "m2"),),
        evaluation_set=evaluation,
        execution_config=_execution_config(),
        policy=PromotionPolicy(
            primary_metric=TASK_PASS_METRIC,
            minimum_replays=2,
        ),
        permission_fingerprint="permissions-v1",
    )

    with pytest.raises(ValueError, match="pass evidence"):
        benchmark_observations(
            forged,
            definition=definition,
            evaluation_set=evaluation,
        )


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


def test_observation_boundary_rejects_development_evidence_even_with_direct_definition() -> None:
    evaluation = _evaluation(EvaluationPurpose.DEVELOPMENT)
    candidate = _candidate("candidate", "m1")
    held_out = _evaluation(EvaluationPurpose.HELD_OUT)
    definition = build_experiment_definition(
        experiment_id="promotion",
        champion=candidate,
        challengers=(_candidate("other", "m2"),),
        evaluation_set=held_out,
        execution_config=_execution_config(),
        policy=PromotionPolicy(primary_metric=QUALITY_METRIC, minimum_replays=2),
        permission_fingerprint="permissions-v1",
    )
    direct_definition = replace(
        definition,
        replays=tuple(
            replace(
                replay,
                dataset_ref=(
                    f"model-evaluation:{evaluation.evaluation_set_id}:"
                    f"sha256:{evaluation.content_sha256}"
                ),
                dataset_version=evaluation.version,
            )
            for replay in definition.replays
        ),
    )
    report = _report(
        candidate,
        evaluation,
        quality=(1.0, 1.0),
        latency=(10.0, 10.0),
    )

    with pytest.raises(ValueError, match="held-out"):
        benchmark_observations(
            report,
            definition=direct_definition,
            evaluation_set=evaluation,
        )


def test_failure_attempt_latency_is_not_projected_as_promotion_latency() -> None:
    evaluation = _evaluation()
    candidate = _candidate("candidate", "m1")
    definition = build_experiment_definition(
        experiment_id="promotion",
        champion=candidate,
        challengers=(_candidate("other", "m2"),),
        evaluation_set=evaluation,
        execution_config=_execution_config(),
        policy=PromotionPolicy(
            primary_metric=COMPLETION_METRIC,
            minimum_replays=2,
            guardrails=(MetricRule(metric=LATENCY_METRIC, higher_is_better=False),),
        ),
        permission_fingerprint="permissions-v1",
    )
    report = _report(
        candidate,
        evaluation,
        quality=(1.0, 1.0),
        latency=(10.0, 20.0),
    )
    failed = replace(
        report.case_results[1],
        completion_succeeded=False,
        passed=False,
        score=0.0,
        response_sha256=None,
        error_code=ModelErrorCode.UNAVAILABLE,
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
    )
    report = replace(
        report,
        case_results=(report.case_results[0], failed),
        weighted_quality_score=0.5,
        task_pass_rate=0.5,
        completion_rate=0.5,
        mean_latency_ms=10.0,
        p95_latency_ms=10.0,
    )

    observations = benchmark_observations(
        report,
        definition=definition,
        evaluation_set=evaluation,
    )

    assert not any(
        item.replay_id == failed.case_id and item.metric == LATENCY_METRIC
        for item in observations
    )


def test_bridge_rejects_cross_execution_config_rebinding() -> None:
    candidate = _candidate("candidate", "m1")
    evaluation = _evaluation()
    trusted_config = _execution_config(timeout_seconds=60.0, temperature=0.0)
    other_config = _execution_config(timeout_seconds=30.0, temperature=0.5)
    definition = build_experiment_definition(
        experiment_id="config-substitution",
        champion=candidate,
        challengers=(_candidate("other", "m2"),),
        evaluation_set=evaluation,
        execution_config=trusted_config,
        policy=PromotionPolicy(primary_metric=QUALITY_METRIC, minimum_replays=2),
        permission_fingerprint="permissions-v1",
    )
    report = _report(
        candidate,
        evaluation,
        quality=(1.0, 1.0),
        latency=(10.0, 10.0),
        execution_config=other_config,
    )

    with pytest.raises(ValueError, match="candidate evidence"):
        benchmark_observations(
            report,
            definition=definition,
            evaluation_set=evaluation,
        )


class _ExecutionConfigAlias(BenchmarkExecutionConfig):
    @property
    def evidence_sha256(self):
        raise AssertionError("behavioral config property executed")


def test_bridge_rejects_execution_config_subclass_before_digest_access() -> None:
    with pytest.raises(TypeError, match="exact BenchmarkExecutionConfig"):
        build_experiment_definition(
            experiment_id="config-carrier",
            champion=_candidate("champion", "m1"),
            challengers=(_candidate("challenger", "m2"),),
            evaluation_set=_evaluation(),
            execution_config=_ExecutionConfigAlias(),
            policy=PromotionPolicy(primary_metric=QUALITY_METRIC, minimum_replays=2),
            permission_fingerprint="permissions-v1",
        )


class _HostileBridgeEnvelope:
    @property
    def purpose(self):
        raise AssertionError("bridge envelope property executed")


class _HostilePermission(str):
    def strip(self, *args, **kwargs):
        del args, kwargs
        raise AssertionError("permission string behavior executed")


def test_definition_bridge_fences_experiment_id_before_behavior() -> None:
    with pytest.raises(TypeError, match="experiment_id must be canonical text"):
        build_experiment_definition(
            experiment_id=_HostilePermission("promotion"),
            champion=_candidate("champion", "m1"),
            challengers=(_candidate("challenger", "m2"),),
            evaluation_set=_evaluation(),
            execution_config=_execution_config(),
            policy=PromotionPolicy(
                primary_metric=QUALITY_METRIC,
                minimum_replays=2,
            ),
            permission_fingerprint="permissions-v1",
        )


def test_definition_bridge_fences_envelopes_before_behavior() -> None:
    policy = PromotionPolicy(primary_metric=QUALITY_METRIC, minimum_replays=2)
    candidate = _candidate("champion", "m1")
    evaluation = _evaluation(EvaluationPurpose.HELD_OUT)

    with pytest.raises(TypeError, match="champion must be an exact ModelCandidate"):
        build_experiment_definition(
            experiment_id="bridge-envelope",
            champion=_HostileBridgeEnvelope(),  # type: ignore[arg-type]
            challengers=(_candidate("challenger", "m2"),),
            evaluation_set=evaluation,
            execution_config=_execution_config(),
            policy=policy,
            permission_fingerprint="permissions-v1",
        )
    with pytest.raises(TypeError, match="evaluation_set must be an exact EvaluationSet"):
        build_experiment_definition(
            experiment_id="bridge-envelope",
            champion=candidate,
            challengers=(_candidate("challenger", "m2"),),
            evaluation_set=_HostileBridgeEnvelope(),  # type: ignore[arg-type]
            execution_config=_execution_config(),
            policy=policy,
            permission_fingerprint="permissions-v1",
        )
    with pytest.raises(TypeError, match="permission_fingerprint must be canonical text"):
        build_experiment_definition(
            experiment_id="bridge-envelope",
            champion=candidate,
            challengers=(_candidate("challenger", "m2"),),
            evaluation_set=evaluation,
            execution_config=_execution_config(),
            policy=policy,
            permission_fingerprint=_HostilePermission("permissions-v1"),
        )


def test_observation_bridge_fences_envelopes_before_behavior() -> None:
    candidate = _candidate("candidate", "m1")
    evaluation = _evaluation(EvaluationPurpose.HELD_OUT)
    definition = build_experiment_definition(
        experiment_id="bridge-observation-envelope",
        champion=candidate,
        challengers=(_candidate("other", "m2"),),
        evaluation_set=evaluation,
        execution_config=_execution_config(),
        policy=PromotionPolicy(primary_metric=QUALITY_METRIC, minimum_replays=2),
        permission_fingerprint="permissions-v1",
    )
    report = _report(
        candidate,
        evaluation,
        quality=(1.0, 1.0),
        latency=(10.0, 11.0),
    )

    with pytest.raises(TypeError, match="report must be an exact CandidateBenchmarkReport"):
        benchmark_observations(
            _HostileBridgeEnvelope(),  # type: ignore[arg-type]
            definition=definition,
            evaluation_set=evaluation,
        )
    with pytest.raises(TypeError, match="definition must be an exact ExperimentDefinition"):
        benchmark_observations(
            report,
            definition=_HostileBridgeEnvelope(),  # type: ignore[arg-type]
            evaluation_set=evaluation,
        )
