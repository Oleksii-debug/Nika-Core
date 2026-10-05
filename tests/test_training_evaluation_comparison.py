from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.experiments import (
    ExperimentEngine,
    ExperimentStatus,
    InMemoryExperimentRepository,
    PromotionPolicy,
    SQLiteExperimentRepository,
)
from nika_core.model_artifacts import (
    ModelArtifactDescriptor,
    ModelArtifactKind,
    ModelIntegrityBasis,
)
from nika_core.model_engineering import (
    BenchmarkExecutionConfig,
    EvaluationCase,
    EvaluationPurpose,
    EvaluationSet,
    ModelCandidate,
    QUALITY_METRIC,
    benchmark_observations,
    build_experiment_definition,
)
from nika_core.model_gateway.contracts import (
    ModelMessage,
    ModelResponse,
    ModelUsage,
    PrivacyClass,
    ProviderKind,
)
from nika_core.training_champion_evaluation import (
    bind_champion_artifact_for_evaluation,
    run_attested_champion_benchmark,
)
from nika_core.training_evaluation_attestation import (
    AttestedModelCompletionResult,
    LoadedModelArtifactAttestation,
)
from nika_core.training_evaluation_binding import TrainingEvaluationBinding
from nika_core.training_evaluation_comparison import run_attested_old_vs_new_comparison
from nika_core.training_evaluation_execution import run_attested_challenger_benchmark


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


_BASE_BYTES = b"base"
_BASE_SHA256 = _sha(_BASE_BYTES)
_CHALLENGER_SHA256 = _sha(b"challenger")
_CHAMPION_ATTESTOR_ID = "test-champion-attestor"
_CHAMPION_ATTESTOR_SHA256 = _sha(b"champion-attestor")
_CHALLENGER_ATTESTOR_ID = "test-challenger-attestor"
_CHALLENGER_ATTESTOR_SHA256 = _sha(b"challenger-attestor")


def _evaluation_set() -> EvaluationSet:
    return EvaluationSet(
        evaluation_set_id="loop-c-held-out",
        version="2026-10-05.v1",
        provenance_ref="dataset:loop-c-held-out",
        license_ref="license:internal-eval",
        purpose=EvaluationPurpose.HELD_OUT,
        privacy=PrivacyClass.PRIVATE,
        cases=(
            EvaluationCase(
                case_id="case-1",
                messages=(ModelMessage(role="user", content="question one"),),
                expected_text="answer",
            ),
            EvaluationCase(
                case_id="case-2",
                messages=(ModelMessage(role="user", content="question two"),),
                expected_text="answer",
            ),
        ),
    )


def _training_binding(evaluation_set: EvaluationSet) -> TrainingEvaluationBinding:
    return TrainingEvaluationBinding(
        job_id="training-job-1",
        base_candidate_id="models/base",
        challenger_candidate_id="models/challenger",
        challenger_provider_id="ollama",
        challenger_model_id="challenger-model",
        base_sha256=_BASE_SHA256,
        challenger_sha256=_CHALLENGER_SHA256,
        candidate_artifact_ref="models/challenger",
        frozen_package_sha256=_sha(b"package"),
        evaluation_set_sha256=evaluation_set.content_sha256,
        descriptor_digest=_sha(b"challenger-descriptor"),
        descriptor_registry_key=_sha(b"challenger-registry"),
        challenger_size_bytes=len(b"challenger"),
    )


def _champion() -> ModelCandidate:
    return ModelCandidate(
        candidate_id="models/base",
        provider_id="ollama",
        provider_kind=ProviderKind.LOCAL,
        request_model="base-model",
        expected_response_model="base-model",
        engine_provenance_ref="engine:ollama",
        engine_license_ref="license:ollama",
        model_provenance_ref="model:base",
        model_license_ref="license:base",
        model_sha256=_BASE_SHA256,
    )


def _challenger() -> ModelCandidate:
    return ModelCandidate(
        candidate_id="models/challenger",
        provider_id="ollama",
        provider_kind=ProviderKind.LOCAL,
        request_model="challenger-model",
        expected_response_model="challenger-model",
        engine_provenance_ref="engine:ollama",
        engine_license_ref="license:ollama",
        model_provenance_ref="model:challenger",
        model_license_ref="license:challenger",
        model_sha256=_CHALLENGER_SHA256,
    )


def _champion_descriptor() -> ModelArtifactDescriptor:
    return ModelArtifactDescriptor(
        kind=ModelArtifactKind.EXTERNAL_LOCAL,
        provider_id="ollama",
        model_id="base-model",
        source_reference="model:base",
        license_reference="license:base",
        integrity_basis=ModelIntegrityBasis.SHA256,
        sha256=_BASE_SHA256,
        size_bytes=len(_BASE_BYTES),
    )


class _ChampionPort:
    def __init__(self, text: str) -> None:
        self.text = text

    async def complete_attested(
        self,
        request,
        *,
        binding: TrainingEvaluationBinding,
    ) -> AttestedModelCompletionResult:
        return AttestedModelCompletionResult(
            response=ModelResponse(
                request_id=request.request_id,
                text=self.text,
                provider_id=binding.challenger_provider_id,
                provider_kind=ProviderKind.LOCAL,
                model=binding.challenger_model_id,
                usage=ModelUsage(input_tokens=2, output_tokens=1, total_tokens=3),
            ),
            attestation=LoadedModelArtifactAttestation(
                request_id=request.request_id,
                binding_sha256=binding.binding_sha256,
                provider_id=binding.challenger_provider_id,
                model_id=binding.challenger_model_id,
                artifact_sha256=binding.challenger_sha256,
                descriptor_digest=binding.descriptor_digest,
                attestor_id=_CHAMPION_ATTESTOR_ID,
                attestor_sha256=_CHAMPION_ATTESTOR_SHA256,
            ),
        )


class _ChallengerPort:
    def __init__(self, text: str) -> None:
        self.text = text

    async def complete_attested(
        self,
        request,
        *,
        binding: TrainingEvaluationBinding,
    ) -> AttestedModelCompletionResult:
        return AttestedModelCompletionResult(
            response=ModelResponse(
                request_id=request.request_id,
                text=self.text,
                provider_id=binding.challenger_provider_id,
                provider_kind=ProviderKind.LOCAL,
                model=binding.challenger_model_id,
                usage=ModelUsage(input_tokens=2, output_tokens=1, total_tokens=3),
            ),
            attestation=LoadedModelArtifactAttestation(
                request_id=request.request_id,
                binding_sha256=binding.binding_sha256,
                provider_id=binding.challenger_provider_id,
                model_id=binding.challenger_model_id,
                artifact_sha256=binding.challenger_sha256,
                descriptor_digest=binding.descriptor_digest,
                attestor_id=_CHALLENGER_ATTESTOR_ID,
                attestor_sha256=_CHALLENGER_ATTESTOR_SHA256,
            ),
        )


async def _attested_results(
    tmp_path,
    *,
    champion_text: str = "old-answer",
    challenger_text: str = "answer",
):
    evaluation = _evaluation_set()
    training_binding = _training_binding(evaluation)
    base_path = tmp_path / "base-model.bin"
    base_path.write_bytes(_BASE_BYTES)
    champion_binding = bind_champion_artifact_for_evaluation(
        training_binding=training_binding,
        champion=_champion(),
        descriptor=_champion_descriptor(),
        candidate_path=base_path,
        allowed_root=tmp_path,
    )
    champion_result = await run_attested_champion_benchmark(
        training_binding=training_binding,
        champion_binding=champion_binding,
        champion=_champion(),
        evaluation_set=evaluation,
        effect_port=_ChampionPort(champion_text),
        expected_attestor_id=_CHAMPION_ATTESTOR_ID,
        expected_attestor_sha256=_CHAMPION_ATTESTOR_SHA256,
        timeout_seconds=5,
        temperature=0,
    )
    challenger_result = await run_attested_challenger_benchmark(
        binding=training_binding,
        challenger=_challenger(),
        evaluation_set=evaluation,
        effect_port=_ChallengerPort(challenger_text),
        expected_attestor_id=_CHALLENGER_ATTESTOR_ID,
        expected_attestor_sha256=_CHALLENGER_ATTESTOR_SHA256,
        timeout_seconds=5,
        temperature=0,
    )
    return evaluation, champion_result, challenger_result


def _config() -> BenchmarkExecutionConfig:
    return BenchmarkExecutionConfig(
        timeout_seconds=5,
        temperature=0,
        scorer_id="exact-match-nfc-v1",
    )


def _policy() -> PromotionPolicy:
    return PromotionPolicy(
        primary_metric=QUALITY_METRIC,
        minimum_improvement=0.5,
        minimum_replays=2,
    )


@pytest.mark.asyncio
async def test_attested_old_vs_new_comparison_promotes_only_from_both_attested_runs(
    tmp_path,
) -> None:
    evaluation, champion_result, challenger_result = await _attested_results(tmp_path)
    repository = InMemoryExperimentRepository()

    result = run_attested_old_vs_new_comparison(
        champion_result=champion_result,
        challenger_result=challenger_result,
        evaluation_set=evaluation,
        execution_config=_config(),
        policy=_policy(),
        permission_fingerprint="perm:test",
        experiment_id="training-job-1-old-vs-new",
        repository=repository,
    )

    assert result.experiment_snapshot.status is ExperimentStatus.PROMOTED
    assert result.experiment_snapshot.selected_candidate_id == "models/challenger"
    assert result.experiment_snapshot.previous_champion_id == "models/base"
    assert len(result.experiment_snapshot.observations) == 4
    assert result.evidence_payload()["observation_count"] == 4
    assert len(result.evidence_sha256) == 64


@pytest.mark.asyncio
async def test_comparison_is_idempotent_after_terminal_persistence(tmp_path) -> None:
    evaluation, champion_result, challenger_result = await _attested_results(tmp_path)
    repository = InMemoryExperimentRepository()
    kwargs = dict(
        champion_result=champion_result,
        challenger_result=challenger_result,
        evaluation_set=evaluation,
        execution_config=_config(),
        policy=_policy(),
        permission_fingerprint="perm:test",
        experiment_id="training-job-1-old-vs-new",
        repository=repository,
    )

    first = run_attested_old_vs_new_comparison(**kwargs)
    second = run_attested_old_vs_new_comparison(**kwargs)

    assert first.evidence_sha256 == second.evidence_sha256
    assert len(second.experiment_snapshot.observations) == 4


@pytest.mark.asyncio
async def test_comparison_resumes_partial_experiment_without_duplicate_observations(
    tmp_path,
) -> None:
    evaluation, champion_result, challenger_result = await _attested_results(tmp_path)
    repository = InMemoryExperimentRepository()
    config = _config()
    policy = _policy()
    definition = build_experiment_definition(
        experiment_id="training-job-1-old-vs-new",
        champion=champion_result.report.candidate,
        challengers=(challenger_result.report.candidate,),
        evaluation_set=evaluation,
        execution_config=config,
        policy=policy,
        permission_fingerprint="perm:test",
    )
    observations = (
        *benchmark_observations(
            champion_result.report,
            definition=definition,
            evaluation_set=evaluation,
        ),
        *benchmark_observations(
            challenger_result.report,
            definition=definition,
            evaluation_set=evaluation,
        ),
    )
    engine = ExperimentEngine(repository)
    engine.create(definition)
    engine.start(definition.experiment_id)
    engine.record(definition.experiment_id, observations[0])

    result = run_attested_old_vs_new_comparison(
        champion_result=champion_result,
        challenger_result=challenger_result,
        evaluation_set=evaluation,
        execution_config=config,
        policy=policy,
        permission_fingerprint="perm:test",
        experiment_id=definition.experiment_id,
        repository=repository,
    )

    assert result.experiment_snapshot.status is ExperimentStatus.PROMOTED
    assert len(result.experiment_snapshot.observations) == 4


@pytest.mark.asyncio
async def test_comparison_rejects_conflicting_persisted_observation(tmp_path) -> None:
    evaluation, champion_result, challenger_result = await _attested_results(tmp_path)
    repository = InMemoryExperimentRepository()
    config = _config()
    policy = _policy()
    definition = build_experiment_definition(
        experiment_id="training-job-1-old-vs-new",
        champion=champion_result.report.candidate,
        challengers=(challenger_result.report.candidate,),
        evaluation_set=evaluation,
        execution_config=config,
        policy=policy,
        permission_fingerprint="perm:test",
    )
    expected = benchmark_observations(
        champion_result.report,
        definition=definition,
        evaluation_set=evaluation,
    )[0]
    conflicting = replace(expected, value=0.25)
    engine = ExperimentEngine(repository)
    engine.create(definition)
    engine.start(definition.experiment_id)
    engine.record(definition.experiment_id, conflicting)

    with pytest.raises(ValueError, match="conflicts with attested benchmark"):
        run_attested_old_vs_new_comparison(
            champion_result=champion_result,
            challenger_result=challenger_result,
            evaluation_set=evaluation,
            execution_config=config,
            policy=policy,
            permission_fingerprint="perm:test",
            experiment_id=definition.experiment_id,
            repository=repository,
        )


@pytest.mark.asyncio
async def test_comparison_rejects_cross_execution_config_evidence_before_persistence(
    tmp_path,
) -> None:
    evaluation, champion_result, challenger_result = await _attested_results(tmp_path)
    repository = InMemoryExperimentRepository()
    mismatched = BenchmarkExecutionConfig(
        timeout_seconds=6,
        temperature=0,
        scorer_id="exact-match-nfc-v1",
    )

    with pytest.raises(ValueError, match="execution configuration"):
        run_attested_old_vs_new_comparison(
            champion_result=champion_result,
            challenger_result=challenger_result,
            evaluation_set=evaluation,
            execution_config=mismatched,
            policy=_policy(),
            permission_fingerprint="perm:test",
            experiment_id="training-job-1-old-vs-new",
            repository=repository,
        )

    with pytest.raises(KeyError):
        repository.get("training-job-1-old-vs-new")


@pytest.mark.asyncio
async def test_comparison_rejects_altered_held_out_set_before_persistence(tmp_path) -> None:
    evaluation, champion_result, challenger_result = await _attested_results(tmp_path)
    repository = InMemoryExperimentRepository()
    altered = replace(evaluation, version="2026-10-05.v2")

    with pytest.raises(ValueError, match="evaluation set does not match training authority"):
        run_attested_old_vs_new_comparison(
            champion_result=champion_result,
            challenger_result=challenger_result,
            evaluation_set=altered,
            execution_config=_config(),
            policy=_policy(),
            permission_fingerprint="perm:test",
            experiment_id="training-job-1-old-vs-new",
            repository=repository,
        )

    with pytest.raises(KeyError):
        repository.get("training-job-1-old-vs-new")


@pytest.mark.asyncio
async def test_comparison_resumes_partial_evidence_after_sqlite_restart(tmp_path) -> None:
    evaluation, champion_result, challenger_result = await _attested_results(tmp_path)
    config = _config()
    policy = _policy()
    definition = build_experiment_definition(
        experiment_id="training-job-1-old-vs-new",
        champion=champion_result.report.candidate,
        challengers=(challenger_result.report.candidate,),
        evaluation_set=evaluation,
        execution_config=config,
        policy=policy,
        permission_fingerprint="perm:test",
    )
    observations = (
        *benchmark_observations(
            champion_result.report,
            definition=definition,
            evaluation_set=evaluation,
        ),
        *benchmark_observations(
            challenger_result.report,
            definition=definition,
            evaluation_set=evaluation,
        ),
    )
    database = tmp_path / "nika.db"
    first_store = SQLiteStore(database)
    first_store.initialize()
    first_repository = SQLiteExperimentRepository(first_store)
    first_engine = ExperimentEngine(first_repository)
    first_engine.create(definition)
    first_engine.start(definition.experiment_id)
    first_engine.record(definition.experiment_id, observations[0])

    reopened_store = SQLiteStore(database)
    reopened_store.initialize()
    reopened_repository = SQLiteExperimentRepository(reopened_store)
    result = run_attested_old_vs_new_comparison(
        champion_result=champion_result,
        challenger_result=challenger_result,
        evaluation_set=evaluation,
        execution_config=config,
        policy=policy,
        permission_fingerprint="perm:test",
        experiment_id=definition.experiment_id,
        repository=reopened_repository,
    )

    assert result.experiment_snapshot.status is ExperimentStatus.PROMOTED
    assert len(result.experiment_snapshot.observations) == 4
    recovered = reopened_repository.get(definition.experiment_id)
    assert recovered == result.experiment_snapshot


@pytest.mark.asyncio
async def test_attested_comparison_keeps_champion_when_improvement_threshold_is_not_met(
    tmp_path,
) -> None:
    evaluation, champion_result, challenger_result = await _attested_results(
        tmp_path,
        champion_text="answer",
        challenger_text="answer",
    )
    repository = InMemoryExperimentRepository()

    result = run_attested_old_vs_new_comparison(
        champion_result=champion_result,
        challenger_result=challenger_result,
        evaluation_set=evaluation,
        execution_config=_config(),
        policy=_policy(),
        permission_fingerprint="perm:test",
        experiment_id="training-job-1-no-promotion",
        repository=repository,
    )

    assert result.experiment_snapshot.status is ExperimentStatus.COMPLETED
    assert result.experiment_snapshot.selected_candidate_id == "models/base"
    assert result.experiment_snapshot.previous_champion_id == "models/base"


@pytest.mark.asyncio
async def test_comparison_rejects_reuse_after_canonical_rollback(tmp_path) -> None:
    evaluation, champion_result, challenger_result = await _attested_results(tmp_path)
    repository = InMemoryExperimentRepository()
    kwargs = dict(
        champion_result=champion_result,
        challenger_result=challenger_result,
        evaluation_set=evaluation,
        execution_config=_config(),
        policy=_policy(),
        permission_fingerprint="perm:test",
        experiment_id="training-job-1-rolled-back",
        repository=repository,
    )
    first = run_attested_old_vs_new_comparison(**kwargs)
    assert first.experiment_snapshot.status is ExperimentStatus.PROMOTED
    rolled_back = ExperimentEngine(repository).rollback("training-job-1-rolled-back")
    assert rolled_back.status is ExperimentStatus.ROLLED_BACK

    with pytest.raises(ValueError, match="rolled-back experiment"):
        run_attested_old_vs_new_comparison(**kwargs)


@pytest.mark.asyncio
async def test_comparison_receipt_rejects_forged_challenger_training_authority(
    tmp_path,
) -> None:
    evaluation, champion_result, challenger_result = await _attested_results(tmp_path)
    result = run_attested_old_vs_new_comparison(
        champion_result=champion_result,
        challenger_result=challenger_result,
        evaluation_set=evaluation,
        execution_config=_config(),
        policy=_policy(),
        permission_fingerprint="perm:test",
        experiment_id="training-job-1-forgery-check",
        repository=InMemoryExperimentRepository(),
    )
    forged_binding = replace(
        result.challenger_result.binding,
        frozen_package_sha256=_sha(b"forged-package"),
    )
    forged_receipts = tuple(
        replace(receipt, binding_sha256=forged_binding.binding_sha256)
        for receipt in result.challenger_result.case_receipts
    )
    object.__setattr__(result.challenger_result, "binding", forged_binding)
    object.__setattr__(result.challenger_result, "case_receipts", forged_receipts)

    assert result.challenger_result.revalidated().binding == forged_binding
    with pytest.raises(ValueError, match="does not share one training authority"):
        result.revalidated()
