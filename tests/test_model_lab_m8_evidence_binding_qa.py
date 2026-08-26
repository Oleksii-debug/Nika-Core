from __future__ import annotations

from nika_core.experiments import (
    ExperimentEngine,
    ExperimentSnapshot,
    ExperimentStatus,
    InMemoryExperimentRepository,
    MetricObservation,
    PromotionPolicy,
)
from nika_core.model_gateway.contracts import (
    ModelMessage,
    PrivacyClass,
    ProviderKind,
)
from nika_core.model_lab import (
    AttemptStatus,
    BenchmarkAttempt,
    BenchmarkCase,
    BenchmarkRunEvidence,
    BenchmarkSuite,
    MetricValue,
    ModelCandidate,
    build_experiment_definition,
    candidate_identity_sha256,
    metric_observations,
    suite_sha256,
)


_METRIC = "quality.score"


def _candidate(
    *,
    candidate_id: str,
    model: str,
    model_version: str,
    artifact_sha256: str,
) -> ModelCandidate:
    return ModelCandidate(
        candidate_id=candidate_id,
        provider_id="ollama",
        provider_kind=ProviderKind.LOCAL,
        model=model,
        model_version=model_version,
        license_reference="license://reviewed",
        provenance_reference="provenance://reviewed",
        permission_fingerprint="perm-v1",
        artifact_sha256=artifact_sha256,
    )


def _suite(
    *,
    dataset_ref: str,
    dataset_version: str = "1",
    repetitions: int = 1,
) -> BenchmarkSuite:
    return BenchmarkSuite(
        suite_id="shared-suite-id",
        version="1.0.0",
        repetitions=repetitions,
        cases=(
            BenchmarkCase(
                case_id="shared-case-id",
                messages=(ModelMessage(role="user", content="private benchmark prompt"),),
                dataset_ref=dataset_ref,
                dataset_version=dataset_version,
                scorer_id="qa-score",
                privacy=PrivacyClass.PRIVATE,
                reference_text="expected",
            ),
        ),
    )


def _complete_evidence(
    *,
    candidate: ModelCandidate,
    suite: BenchmarkSuite,
    value: float,
    expected_attempts: int = 1,
) -> BenchmarkRunEvidence:
    case = suite.cases[0]
    return BenchmarkRunEvidence(
        run_id="qa-run",
        candidate=candidate,
        suite_id=suite.suite_id,
        suite_version=suite.version,
        suite_sha256=suite_sha256(suite),
        expected_attempts=expected_attempts,
        attempts=(
            BenchmarkAttempt(
                attempt_id=f"{case.case_id}:1",
                case_id=case.case_id,
                repetition=1,
                status=AttemptStatus.SUCCESS,
                request_id="qa-run:shared-case-id:1",
                prompt_sha256="c" * 64,
                reference_sha256="d" * 64,
                provider_id=candidate.provider_id,
                provider_kind=candidate.provider_kind,
                model=candidate.model,
                wall_latency_ms=1.0,
                metrics=(MetricValue(metric=_METRIC, value=value),),
                response_sha256="e" * 64,
                response_characters=1,
            ),
        ),
    )


def _complete_m8(
    *,
    declared_challenger: ModelCandidate,
    definition_suite: BenchmarkSuite,
    challenger_evidence: BenchmarkRunEvidence,
) -> ExperimentSnapshot:
    champion = _candidate(
        candidate_id="champion",
        model="champion-model",
        model_version="champion-v1",
        artifact_sha256="1" * 64,
    )
    definition = build_experiment_definition(
        experiment_id="qa-model-lab-binding",
        champion=champion,
        challengers=(declared_challenger,),
        suite=definition_suite,
        policy=PromotionPolicy(primary_metric=_METRIC),
    )
    engine = ExperimentEngine(InMemoryExperimentRepository())
    engine.create(definition)
    engine.start(definition.experiment_id)
    engine.record(
        definition.experiment_id,
        MetricObservation(
            candidate_id=champion.candidate_id,
            replay_id=definition_suite.cases[0].case_id,
            metric=_METRIC,
            value=0.0,
        ),
    )
    for observation in metric_observations(challenger_evidence, metrics=(_METRIC,)):
        engine.record(definition.experiment_id, observation)
    return engine.complete(definition.experiment_id)


def test_m8_must_not_promote_evidence_from_different_candidate_identity() -> None:
    trusted_suite = _suite(dataset_ref="dataset://trusted")
    declared = _candidate(
        candidate_id="challenger",
        model="reviewed-model",
        model_version="reviewed-v1",
        artifact_sha256="a" * 64,
    )
    substituted = _candidate(
        candidate_id="challenger",
        model="substituted-model",
        model_version="substituted-v2",
        artifact_sha256="b" * 64,
    )
    assert candidate_identity_sha256(declared) != candidate_identity_sha256(substituted)

    result = _complete_m8(
        declared_challenger=declared,
        definition_suite=trusted_suite,
        challenger_evidence=_complete_evidence(
            candidate=substituted,
            suite=trusted_suite,
            value=1.0,
        ),
    )

    # Security invariant: evidence for a different immutable model identity must not
    # be able to promote the declared challenger merely by reusing candidate_id.
    assert result.status is ExperimentStatus.COMPLETED
    assert result.selected_candidate_id == "champion"


def test_m8_must_not_promote_evidence_from_different_suite_provenance() -> None:
    trusted_suite = _suite(dataset_ref="dataset://trusted", dataset_version="1")
    substituted_suite = _suite(dataset_ref="dataset://substituted", dataset_version="999")
    challenger = _candidate(
        candidate_id="challenger",
        model="reviewed-model",
        model_version="reviewed-v1",
        artifact_sha256="a" * 64,
    )
    assert suite_sha256(trusted_suite) != suite_sha256(substituted_suite)

    result = _complete_m8(
        declared_challenger=challenger,
        definition_suite=trusted_suite,
        challenger_evidence=_complete_evidence(
            candidate=challenger,
            suite=substituted_suite,
            value=1.0,
        ),
    )

    # Provenance invariant: replay_id equality is insufficient authority when the
    # benchmark suite/dataset binding differs from the ExperimentDefinition.
    assert result.status is ExperimentStatus.COMPLETED
    assert result.selected_candidate_id == "champion"


def test_m8_must_not_promote_under_sampled_declared_repetitions() -> None:
    trusted_suite = _suite(dataset_ref="dataset://trusted", repetitions=2)
    challenger = _candidate(
        candidate_id="challenger",
        model="reviewed-model",
        model_version="reviewed-v1",
        artifact_sha256="a" * 64,
    )
    under_sampled = _complete_evidence(
        candidate=challenger,
        suite=trusted_suite,
        value=1.0,
        expected_attempts=1,
    )
    assert trusted_suite.repetitions == 2
    assert under_sampled.complete is True

    result = _complete_m8(
        declared_challenger=challenger,
        definition_suite=trusted_suite,
        challenger_evidence=under_sampled,
    )

    # Coverage invariant: declaring two repetitions must not be satisfiable by one
    # repetition merely because BenchmarkRunEvidence.expected_attempts was lowered.
    assert result.status is ExperimentStatus.COMPLETED
    assert result.selected_candidate_id == "champion"
