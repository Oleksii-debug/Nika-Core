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
from nika_core.kernel.task_queue import TaskQueue
from nika_core.learning_package import FrozenLearningPackage, LearningDataSplit, LearningShard
from nika_core.model_gateway.contracts import (
    ModelMessage,
    ModelResponse,
    ModelUsage,
    PrivacyClass,
    ProviderKind,
)
from nika_core.training_evaluation_champion import (
    bind_champion_for_attested_evaluation,
)
from nika_core.training_evaluation_champion_execution import (
    run_attested_champion_benchmark,
)
from nika_core.training_evaluation_attestation import (
    AttestedModelCompletionResult,
    LoadedModelArtifactAttestation,
)
from nika_core.training_evaluation_binding import TrainingEvaluationBinding
from nika_core.training_evaluation_comparison import run_attested_old_vs_new_comparison
from nika_core.training_evaluation_execution import run_attested_challenger_benchmark
from nika_core.training_materials import TrainingMaterialEvidence, TrainingMaterialSetEvidence
from nika_core.training_model_activation import (
    TrainingModelActivationError,
    activate_attested_training_promotion,
    rollback_attested_training_promotion,
)
from nika_core.training_scale import (
    TrainingScalePlan,
    TrainingScaleTier,
    authorize_training_scale,
    build_scale_progression_proof,
)
from nika_core.training_runtime import (
    ArtifactIdentity,
    TrainingJobSpec,
    TrainingRunEvidence,
    TrainingRunState,
    training_job_fingerprint,
)
from nika_core.v01_model_settings import ModelSetupError, V01ModelSettings


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


_BASE_BYTES = b"base"
_BASE_SHA256 = _sha(_BASE_BYTES)
_CHALLENGER_SHA256 = _sha(b"challenger")
_CHAMPION_ATTESTOR_ID = "test-shared-attestor"
_CHAMPION_ATTESTOR_SHA256 = _sha(b"shared-attestor")
_CHALLENGER_ATTESTOR_ID = _CHAMPION_ATTESTOR_ID
_CHALLENGER_ATTESTOR_SHA256 = _CHAMPION_ATTESTOR_SHA256


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


def _scale_material_evidence(
    evaluation_set: EvaluationSet,
    *,
    base_sha256: str = _BASE_SHA256,
    package_version: str = "1",
    training_records: int = 1,
) -> TrainingMaterialSetEvidence:
    training_body = f"training-{package_version}".encode()
    validation_body = b"validation"
    shards = (
        LearningShard(
            split=LearningDataSplit.TRAINING,
            artifact_sha256=_sha(training_body),
            provenance_sha256=_sha(b"scale-training-provenance"),
            license_evidence_sha256=_sha(b"scale-training-license"),
            record_count=training_records,
            byte_count=len(training_body),
        ),
        LearningShard(
            split=LearningDataSplit.VALIDATION,
            artifact_sha256=_sha(validation_body),
            provenance_sha256=_sha(b"scale-validation-provenance"),
            license_evidence_sha256=_sha(b"scale-validation-license"),
            record_count=1,
            byte_count=len(validation_body),
        ),
    )
    package = FrozenLearningPackage.freeze(
        package_id="comparison-scale-package",
        package_version=package_version,
        base_artifact_sha256=base_sha256,
        selection_policy_sha256=_sha(b"scale-selection"),
        verification_sha256=_sha(b"scale-verification"),
        evaluation_set_sha256=evaluation_set.content_sha256,
        shards=shards,
    )
    return TrainingMaterialSetEvidence.from_package(
        package,
        workspace_sha256=_sha(b"comparison-scale-workspace"),
        materials=tuple(
            TrainingMaterialEvidence.from_shard(shard)
            for shard in shards
        ),
    )


def _comparison_scale_plan(
    evaluation_set: EvaluationSet,
    material_evidence: TrainingMaterialSetEvidence,
) -> TrainingScalePlan:
    pilot_training = next(
        item
        for item in material_evidence.materials
        if item.split is LearningDataSplit.TRAINING
    )
    pilot_validation = next(
        item
        for item in material_evidence.materials
        if item.split is LearningDataSplit.VALIDATION
    )
    return TrainingScalePlan(
        plan_id="comparison-scale",
        evaluation_set_sha256=evaluation_set.content_sha256,
        tiers=(
            TrainingScaleTier(
                tier_id="pilot",
                max_training_records=pilot_training.record_count,
                max_training_bytes=pilot_training.byte_count,
                max_validation_records=pilot_validation.record_count,
                max_validation_bytes=pilot_validation.byte_count,
                max_steps=1,
            ),
            TrainingScaleTier(
                tier_id="small",
                max_training_records=8,
                max_training_bytes=4096,
                max_validation_records=8,
                max_validation_bytes=4096,
                max_steps=8,
            ),
        ),
    )


def _pilot_scale_authorization(
    evaluation_set: EvaluationSet,
    material_evidence: TrainingMaterialSetEvidence,
):
    return authorize_training_scale(
        plan=_comparison_scale_plan(evaluation_set, material_evidence),
        tier_id="pilot",
        job_id="training-job-1",
        base_artifact=ArtifactIdentity("models/base", _BASE_SHA256),
        candidate_artifact_ref="models/challenger",
        material_evidence=material_evidence,
        execution_plan_sha256=_sha(b"training-execution-plan"),
        max_steps=1,
    )


def _training_binding(evaluation_set: EvaluationSet) -> TrainingEvaluationBinding:
    descriptor = _champion_descriptor()
    material_evidence = _scale_material_evidence(evaluation_set)
    scale_authorization = _pilot_scale_authorization(
        evaluation_set,
        material_evidence,
    )
    return TrainingEvaluationBinding(
        job_id="training-job-1",
        base_candidate_id="models/base",
        base_provider_id="ollama",
        base_model_id="base-model",
        challenger_candidate_id="models/challenger",
        challenger_provider_id="ollama",
        challenger_model_id="challenger-model",
        base_sha256=_BASE_SHA256,
        challenger_sha256=_CHALLENGER_SHA256,
        candidate_artifact_ref="models/challenger",
        frozen_package_sha256=material_evidence.package_manifest_sha256,
        scale_authorization_sha256=scale_authorization.authorization_sha256,
        execution_plan_sha256=_sha(b"training-execution-plan"),
        evaluation_set_sha256=evaluation_set.content_sha256,
        base_descriptor_digest=descriptor.descriptor_digest,
        base_descriptor_registry_key=descriptor.registry_key,
        base_size_bytes=descriptor.size_bytes,
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
        binding,
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
        self.calls = 0

    async def complete_attested(
        self,
        request,
        *,
        binding,
    ) -> AttestedModelCompletionResult:
        self.calls += 1
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


async def _attested_results(tmp_path):
    evaluation = _evaluation_set()
    training_binding = _training_binding(evaluation)
    base_path = tmp_path / "base-model.bin"
    base_path.write_bytes(_BASE_BYTES)
    champion_binding = bind_champion_for_attested_evaluation(
        training_binding=training_binding,
        champion=_champion(),
        descriptor=_champion_descriptor(),
        champion_path=base_path,
        allowed_root=tmp_path,
    )
    champion_result = await run_attested_champion_benchmark(
        binding=champion_binding,
        champion=_champion(),
        evaluation_set=evaluation,
        effect_port=_ChampionPort("old-answer"),
        expected_attestor_id=_CHAMPION_ATTESTOR_ID,
        expected_attestor_sha256=_CHAMPION_ATTESTOR_SHA256,
        timeout_seconds=5,
        temperature=0,
    )
    challenger_result = await run_attested_challenger_benchmark(
        binding=training_binding,
        challenger=_challenger(),
        evaluation_set=evaluation,
        effect_port=_ChallengerPort("answer"),
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
    payload = result.evidence_payload()
    assert payload["observation_count"] == 4
    assert payload["attestor_id"] == _CHAMPION_ATTESTOR_ID
    assert payload["attestor_sha256"] == _CHAMPION_ATTESTOR_SHA256
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
async def test_comparison_rejects_terminal_outcome_that_disagrees_with_engine(
    tmp_path,
) -> None:
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
    result = run_attested_old_vs_new_comparison(**kwargs)
    assert result.experiment_snapshot.status is ExperimentStatus.PROMOTED
    forged = replace(
        result.experiment_snapshot,
        status=ExperimentStatus.COMPLETED,
        selected_candidate_id="models/base",
    )
    repository.save(forged)

    with pytest.raises(
        ValueError,
        match="canonical Experiment Engine decision",
    ):
        run_attested_old_vs_new_comparison(**kwargs)

    assert repository.get("training-job-1-old-vs-new") == forged


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
    conflicting = replace(
        observations[-1],
        value=float(observations[-1].value) + 0.25,
    )
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

    persisted = repository.get(definition.experiment_id)
    assert persisted.status is ExperimentStatus.RUNNING
    assert persisted.observations == (conflicting,)


@pytest.mark.asyncio
async def test_comparison_rejects_different_attestor_before_persistence(
    tmp_path,
) -> None:
    evaluation, champion_result, challenger_result = await _attested_results(tmp_path)
    other_id = "different-attestor"
    other_sha256 = _sha(b"different-attestor")
    object.__setattr__(challenger_result, "attestor_id", other_id)
    object.__setattr__(challenger_result, "attestor_sha256", other_sha256)
    object.__setattr__(
        challenger_result,
        "case_receipts",
        tuple(
            replace(
                receipt,
                attestor_id=other_id,
                attestor_sha256=other_sha256,
            )
            for receipt in challenger_result.case_receipts
        ),
    )
    challenger_result.revalidated()
    repository = InMemoryExperimentRepository()

    with pytest.raises(ValueError, match="one attestor authority"):
        run_attested_old_vs_new_comparison(
            champion_result=champion_result,
            challenger_result=challenger_result,
            evaluation_set=evaluation,
            execution_config=_config(),
            policy=_policy(),
            permission_fingerprint="perm:test",
            experiment_id="training-job-1-old-vs-new",
            repository=repository,
        )

    with pytest.raises(KeyError):
        repository.get("training-job-1-old-vs-new")


@pytest.mark.asyncio
async def test_comparison_result_rejects_terminal_outcome_mutation(tmp_path) -> None:
    evaluation, champion_result, challenger_result = await _attested_results(tmp_path)
    result = run_attested_old_vs_new_comparison(
        champion_result=champion_result,
        challenger_result=challenger_result,
        evaluation_set=evaluation,
        execution_config=_config(),
        policy=_policy(),
        permission_fingerprint="perm:test",
        experiment_id="training-job-1-old-vs-new",
        repository=InMemoryExperimentRepository(),
    )
    assert result.experiment_snapshot.status is ExperimentStatus.PROMOTED
    object.__setattr__(
        result.experiment_snapshot,
        "status",
        ExperimentStatus.COMPLETED,
    )
    object.__setattr__(
        result.experiment_snapshot,
        "selected_candidate_id",
        "models/base",
    )

    with pytest.raises(
        ValueError,
        match="canonical Experiment Engine decision",
    ):
        result.evidence_payload()


@pytest.mark.asyncio
async def test_comparison_result_rejects_attestor_authority_substitution(
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
        experiment_id="training-job-1-old-vs-new",
        repository=InMemoryExperimentRepository(),
    )
    other_id = "different-attestor"
    other_sha256 = _sha(b"different-attestor")
    nested = result.challenger_benchmark
    object.__setattr__(nested, "attestor_id", other_id)
    object.__setattr__(nested, "attestor_sha256", other_sha256)
    object.__setattr__(
        nested,
        "case_receipts",
        tuple(
            replace(
                receipt,
                attestor_id=other_id,
                attestor_sha256=other_sha256,
            )
            for receipt in nested.case_receipts
        ),
    )
    nested.revalidated()

    with pytest.raises(ValueError, match="one attestor authority"):
        result.evidence_payload()


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
async def test_comparison_rejects_running_terminal_fields_before_writes(tmp_path) -> None:
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
    engine = ExperimentEngine(repository)
    engine.create(definition)
    running = engine.start(definition.experiment_id)
    forged = replace(
        running,
        selected_candidate_id=challenger_result.report.candidate.candidate_id,
    )
    repository.save(forged)

    with pytest.raises(ValueError, match="terminal decision fields"):
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

    assert repository.get(definition.experiment_id) == forged


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
async def test_comparison_revalidation_rejects_mutated_champion_training_authority(
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
        experiment_id="training-job-1-old-vs-new",
        repository=InMemoryExperimentRepository(),
    )
    object.__setattr__(
        result.champion_benchmark.binding,
        "training_binding_sha256",
        "0" * 64,
    )

    with pytest.raises(ValueError, match="challenger training authority"):
        result.evidence_payload()


@pytest.mark.asyncio
async def test_comparison_hash_properties_are_derived_from_nested_authorities(
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
        experiment_id="training-job-1-old-vs-new",
        repository=InMemoryExperimentRepository(),
    )

    assert result.training_binding_sha256 == challenger_result.binding.binding_sha256
    assert result.champion_binding_sha256 == champion_result.binding.binding_sha256
    assert result.champion_benchmark_sha256 == champion_result.evidence_sha256
    assert result.challenger_benchmark_sha256 == challenger_result.evidence_sha256
    with pytest.raises(AttributeError):
        object.__setattr__(result, "training_binding_sha256", "0" * 64)




def _configured_model_settings(tmp_path, *, model: str = "base-model"):
    store = SQLiteStore(tmp_path / "activation-settings.db")
    store.initialize()
    settings = V01ModelSettings(store)
    configured = settings.configure(
        {
            "route_kind": "ollama",
            "provider_id": "ollama",
            "model": model,
            "base_url": "http://localhost:11434",
            "private_data_allowed": False,
            "timeout_seconds": 30,
            "revision": 0,
        }
    )
    assert configured.status == "completed"
    return store, settings


async def _promoted_comparison(tmp_path):
    evaluation, champion_result, challenger_result = await _attested_results(tmp_path)
    return run_attested_old_vs_new_comparison(
        champion_result=champion_result,
        challenger_result=challenger_result,
        evaluation_set=evaluation,
        execution_config=_config(),
        policy=_policy(),
        permission_fingerprint="perm:activation",
        experiment_id="training-job-1-activation",
        repository=InMemoryExperimentRepository(),
    )


@pytest.mark.asyncio
async def test_promoted_pilot_authorizes_exact_next_scale_tier(tmp_path) -> None:
    evaluation = _evaluation_set()
    pilot_materials = _scale_material_evidence(evaluation)
    plan = _comparison_scale_plan(evaluation, pilot_materials)
    execution_plan_sha256 = _sha(b"training-execution-plan")
    pilot_authorization = _pilot_scale_authorization(
        evaluation,
        pilot_materials,
    )
    pilot_spec = TrainingJobSpec(
        job_id="training-job-1",
        task_id="training-task-1",
        project_id="project-1",
        owner_id="owner-1",
        base_artifact=pilot_authorization.base_artifact,
        frozen_package_sha256=pilot_materials.package_manifest_sha256,
        training_material_sha256=pilot_materials.training_material_sha256,
        scale_authorization_sha256=pilot_authorization.authorization_sha256,
        candidate_artifact_ref="models/challenger",
        max_steps=1,
    )
    pilot_run = TrainingRunEvidence(
        job_id=pilot_spec.job_id,
        state=TrainingRunState.COMPLETED,
        next_step=1,
        base_artifact=pilot_spec.base_artifact,
        frozen_package_sha256=pilot_spec.frozen_package_sha256,
        training_material_sha256=pilot_spec.training_material_sha256,
        scale_authorization_sha256=pilot_spec.scale_authorization_sha256,
        execution_plan_sha256=execution_plan_sha256,
        job_fingerprint=training_job_fingerprint(
            pilot_spec,
            execution_plan_sha256=execution_plan_sha256,
        ),
        candidate_artifact_ref=pilot_spec.candidate_artifact_ref,
        candidate_sha256=_CHALLENGER_SHA256,
        checkpoint_id="pilot-checkpoint",
    )
    comparison = await _promoted_comparison(tmp_path)
    proof = build_scale_progression_proof(
        plan=plan,
        authorization=pilot_authorization,
        run=pilot_run,
        comparison=comparison,
    )

    next_materials = _scale_material_evidence(
        evaluation,
        base_sha256=_CHALLENGER_SHA256,
        package_version="2",
        training_records=2,
    )
    next_authorization = authorize_training_scale(
        plan=plan,
        tier_id="small",
        job_id="training-job-2",
        base_artifact=ArtifactIdentity("models/challenger", _CHALLENGER_SHA256),
        candidate_artifact_ref="models/challenger-2",
        material_evidence=next_materials,
        execution_plan_sha256=_sha(b"next-training-plan"),
        max_steps=4,
        progression_proof=proof,
    )

    assert proof.tier_index == 0
    assert proof.candidate_sha256 == _CHALLENGER_SHA256
    assert next_authorization.tier_index == 1
    assert next_authorization.progression_proof == proof
    assert next_authorization.base_artifact.sha256 == _CHALLENGER_SHA256


@pytest.mark.asyncio
async def test_attested_promotion_activates_future_tasks_and_rolls_back_durably(
    tmp_path,
) -> None:
    result = await _promoted_comparison(tmp_path)
    store, settings = _configured_model_settings(tmp_path)

    old_task = TaskQueue(store).create(
        workspace_id="default",
        agent_id="nika.default",
        payload=settings.prepare_task_payload({"command": "keep old route"}),
    )

    activation_port = _ChallengerPort("activation-ok")
    receipt = await activate_attested_training_promotion(
        result=result,
        settings=settings,
        expected_revision=1,
        effect_port=activation_port,
    )

    assert receipt.decision_sha256 == result.evidence_sha256
    assert (
        receipt.binding_sha256
        == result.challenger_benchmark.binding.binding_sha256
    )
    assert receipt.activated_revision == 2
    assert receipt.rollback_revision is None
    assert len(receipt.activation_request_sha256) == 64
    assert len(receipt.activation_attestation_sha256) == 64
    training = result.challenger_benchmark.binding.revalidated()
    assert receipt.base_artifact_sha256 == training.base_sha256
    assert receipt.base_descriptor_digest == training.base_descriptor_digest
    assert receipt.challenger_artifact_sha256 == training.challenger_sha256
    assert receipt.challenger_descriptor_digest == training.descriptor_digest
    assert settings.snapshot()["model"] == "challenger-model"
    assert settings.snapshot()["revision"] == 2

    retried = await activate_attested_training_promotion(
        result=result,
        settings=settings,
        expected_revision=1,
    )
    assert retried == receipt
    assert activation_port.calls == 1
    assert settings.snapshot()["revision"] == 2
    assert settings.for_task(old_task.task_id).model == "base-model"
    new_task = TaskQueue(store).create(
        workspace_id="default",
        agent_id="nika.default",
        payload=settings.prepare_task_payload({"command": "use promoted route"}),
    )
    assert settings.for_task(new_task.task_id).model == "challenger-model"

    restarted = V01ModelSettings(SQLiteStore(store.path))
    assert restarted.snapshot()["model"] == "challenger-model"
    assert restarted.snapshot()["revision"] == 2
    persisted = restarted.promotion_receipt(result.evidence_sha256)
    assert persisted == receipt

    rolled_back = rollback_attested_training_promotion(
        result=result,
        settings=restarted,
        expected_revision=2,
    )
    assert rolled_back.rollback_revision == 3
    assert restarted.snapshot()["model"] == "base-model"
    assert restarted.snapshot()["revision"] == 3

    repeated = rollback_attested_training_promotion(
        result=result,
        settings=restarted,
        expected_revision=2,
    )
    assert repeated == rolled_back
    with pytest.raises(
        TrainingModelActivationError,
        match="rejected by the active route authority",
    ):
        await activate_attested_training_promotion(
            result=result,
            settings=restarted,
            expected_revision=3,
        )

    reopened = V01ModelSettings(SQLiteStore(store.path))
    assert reopened.snapshot()["model"] == "base-model"
    assert reopened.snapshot()["revision"] == 3

    with store.connection() as conn:
        events = [
            row["event_type"]
            for row in conn.execute(
                "SELECT event_type FROM audit_events ORDER BY event_id"
            )
        ]
    assert events.count("v01.model.promoted") == 1
    assert events.count("v01.model.promotion_rolled_back") == 1
    with store.connection() as conn:
        audit_payloads = [
            row["payload_json"]
            for row in conn.execute(
                "SELECT payload_json FROM audit_events ORDER BY event_id"
            )
        ]
    assert "base-model" not in repr(audit_payloads)
    assert "challenger-model" not in repr(audit_payloads)


@pytest.mark.asyncio
async def test_non_promoted_attested_comparison_cannot_activate_model(tmp_path) -> None:
    evaluation, champion_result, challenger_result = await _attested_results(tmp_path)
    result = run_attested_old_vs_new_comparison(
        champion_result=champion_result,
        challenger_result=challenger_result,
        evaluation_set=evaluation,
        execution_config=_config(),
        policy=PromotionPolicy(
            primary_metric=QUALITY_METRIC,
            minimum_improvement=2.0,
            minimum_replays=2,
        ),
        permission_fingerprint="perm:no-promotion",
        experiment_id="training-job-1-no-promotion",
        repository=InMemoryExperimentRepository(),
    )
    _, settings = _configured_model_settings(tmp_path)

    assert result.experiment_snapshot.status is ExperimentStatus.COMPLETED
    with pytest.raises(
        TrainingModelActivationError,
        match="requires a PROMOTED",
    ):
        await activate_attested_training_promotion(
            result=result,
            settings=settings,
            expected_revision=1,
        )
    assert settings.snapshot()["model"] == "base-model"
    assert settings.snapshot()["revision"] == 1


@pytest.mark.asyncio
async def test_activation_rejects_stale_current_champion_without_mutation(tmp_path) -> None:
    result = await _promoted_comparison(tmp_path)
    _, settings = _configured_model_settings(tmp_path, model="manual-current-model")

    with pytest.raises(
        TrainingModelActivationError,
        match="current model route no longer matches",
    ):
        await activate_attested_training_promotion(
            result=result,
            settings=settings,
            expected_revision=1,
        )

    assert settings.snapshot()["model"] == "manual-current-model"
    assert settings.snapshot()["revision"] == 1


@pytest.mark.asyncio
async def test_activation_requires_fresh_loaded_model_attestation(tmp_path) -> None:
    result = await _promoted_comparison(tmp_path)
    _, settings = _configured_model_settings(tmp_path)

    with pytest.raises(
        TrainingModelActivationError,
        match="fresh loaded-model attestation is required",
    ):
        await activate_attested_training_promotion(
            result=result,
            settings=settings,
            expected_revision=1,
        )

    assert settings.snapshot()["model"] == "base-model"
    assert settings.snapshot()["revision"] == 1


@pytest.mark.asyncio
async def test_activation_rejects_swapped_loaded_artifact_before_route_mutation(
    tmp_path,
) -> None:
    result = await _promoted_comparison(tmp_path)
    _, settings = _configured_model_settings(tmp_path)

    class _SwappedArtifactPort(_ChallengerPort):
        async def complete_attested(self, request, *, binding):
            attested = await super().complete_attested(request, binding=binding)
            return replace(
                attested,
                attestation=replace(
                    attested.attestation,
                    artifact_sha256=_sha(b"swapped-after-evaluation"),
                ),
            )

    port = _SwappedArtifactPort("activation-ok")
    with pytest.raises(
        TrainingModelActivationError,
        match="fresh loaded-model activation attestation failed",
    ):
        await activate_attested_training_promotion(
            result=result,
            settings=settings,
            expected_revision=1,
            effect_port=port,
        )

    assert port.calls == 1
    assert settings.snapshot()["model"] == "base-model"
    assert settings.snapshot()["revision"] == 1


@pytest.mark.asyncio
async def test_newer_manual_route_blocks_promotion_rollback(tmp_path) -> None:
    result = await _promoted_comparison(tmp_path)
    _, settings = _configured_model_settings(tmp_path)
    await activate_attested_training_promotion(
        result=result,
        settings=settings,
        expected_revision=1,
        effect_port=_ChallengerPort("activation-ok"),
    )
    manual = settings.configure(
        {
            "route_kind": "ollama",
            "provider_id": "ollama",
            "model": "owner-selected-model",
            "base_url": "http://localhost:11434",
            "private_data_allowed": False,
            "timeout_seconds": 30,
            "revision": 2,
        }
    )
    assert manual.status == "completed"

    with pytest.raises(
        TrainingModelActivationError,
        match="rejected by the active route authority",
    ):
        await activate_attested_training_promotion(
            result=result,
            settings=settings,
            expected_revision=3,
        )

    with pytest.raises(
        TrainingModelActivationError,
        match="rollback was rejected",
    ):
        rollback_attested_training_promotion(
            result=result,
            settings=settings,
            expected_revision=3,
        )

    assert settings.snapshot()["model"] == "owner-selected-model"
    assert settings.snapshot()["revision"] == 3


def test_settings_reject_cross_provider_promotion_before_route_mutation(tmp_path) -> None:
    _, settings = _configured_model_settings(tmp_path)
    decision_sha256 = _sha(b"decision")
    binding_sha256 = _sha(b"binding")

    with pytest.raises(ModelSetupError, match="постачальника"):
        settings.activate_promoted_local_model(
            expected_revision=1,
            base_provider_id="ollama",
            base_model_id="base-model",
            challenger_provider_id="foundry-local",
            challenger_model_id="challenger-model",
            decision_sha256=decision_sha256,
            binding_sha256=binding_sha256,
            base_artifact_sha256=_sha(b"base-artifact"),
            base_descriptor_digest=_sha(b"base-descriptor"),
            challenger_artifact_sha256=_sha(b"challenger-artifact"),
            challenger_descriptor_digest=_sha(b"challenger-descriptor"),
            activation_request_sha256=_sha(b"activation-request"),
            activation_attestation_sha256=_sha(b"activation-attestation"),
        )

    assert settings.snapshot()["model"] == "base-model"
    assert settings.snapshot()["revision"] == 1


def test_direct_promotion_retry_keeps_first_committed_activation_proof(tmp_path) -> None:
    _, settings = _configured_model_settings(tmp_path)
    decision_sha256 = _sha(b"race-decision")
    binding_sha256 = _sha(b"race-binding")
    base_artifact_sha256 = _sha(b"race-base-artifact")
    base_descriptor_digest = _sha(b"race-base-descriptor")
    challenger_artifact_sha256 = _sha(b"race-challenger-artifact")
    challenger_descriptor_digest = _sha(b"race-challenger-descriptor")
    first_request = _sha(b"race-first-request")
    first_attestation = _sha(b"race-first-attestation")

    first = settings.activate_promoted_local_model(
        expected_revision=1,
        base_provider_id="ollama",
        base_model_id="base-model",
        challenger_provider_id="ollama",
        challenger_model_id="challenger-model",
        decision_sha256=decision_sha256,
        binding_sha256=binding_sha256,
        base_artifact_sha256=base_artifact_sha256,
        base_descriptor_digest=base_descriptor_digest,
        challenger_artifact_sha256=challenger_artifact_sha256,
        challenger_descriptor_digest=challenger_descriptor_digest,
        activation_request_sha256=first_request,
        activation_attestation_sha256=first_attestation,
    )
    retried = settings.activate_promoted_local_model(
        expected_revision=1,
        base_provider_id="ollama",
        base_model_id="base-model",
        challenger_provider_id="ollama",
        challenger_model_id="challenger-model",
        decision_sha256=decision_sha256,
        binding_sha256=binding_sha256,
        base_artifact_sha256=base_artifact_sha256,
        base_descriptor_digest=base_descriptor_digest,
        challenger_artifact_sha256=challenger_artifact_sha256,
        challenger_descriptor_digest=challenger_descriptor_digest,
        activation_request_sha256=_sha(b"race-second-request"),
        activation_attestation_sha256=_sha(b"race-second-attestation"),
    )

    assert retried == first
    assert retried.activation_request_sha256 == first_request
    assert retried.activation_attestation_sha256 == first_attestation
    assert settings.snapshot()["revision"] == 2


@pytest.mark.asyncio
async def test_v2_promotion_database_migrates_without_fabricating_attestation(
    tmp_path,
) -> None:
    result = await _promoted_comparison(tmp_path)
    store, settings = _configured_model_settings(tmp_path)
    original = await activate_attested_training_promotion(
        result=result,
        settings=settings,
        expected_revision=1,
        effect_port=_ChallengerPort("activation-ok"),
    )
    assert original.activated_revision == 2

    with store.connection() as conn:
        conn.execute(
            "ALTER TABLE v01_model_promotions RENAME TO v01_model_promotions_v3"
        )
        conn.execute(
            "CREATE TABLE v01_model_promotions ("
            "decision_sha256 TEXT PRIMARY KEY, "
            "binding_sha256 TEXT NOT NULL, "
            "base_artifact_sha256 TEXT NOT NULL, "
            "base_descriptor_digest TEXT NOT NULL, "
            "challenger_artifact_sha256 TEXT NOT NULL, "
            "challenger_descriptor_digest TEXT NOT NULL, "
            "previous_selection_id TEXT NOT NULL, "
            "activated_selection_id TEXT NOT NULL, "
            "activated_revision INTEGER NOT NULL CHECK(activated_revision > 0), "
            "rollback_revision INTEGER, "
            "CHECK(rollback_revision IS NULL OR rollback_revision > activated_revision))"
        )
        conn.execute(
            "INSERT INTO v01_model_promotions "
            "(decision_sha256, binding_sha256, base_artifact_sha256, "
            "base_descriptor_digest, challenger_artifact_sha256, "
            "challenger_descriptor_digest, previous_selection_id, "
            "activated_selection_id, activated_revision, rollback_revision) "
            "SELECT decision_sha256, binding_sha256, base_artifact_sha256, "
            "base_descriptor_digest, challenger_artifact_sha256, "
            "challenger_descriptor_digest, previous_selection_id, "
            "activated_selection_id, activated_revision, rollback_revision "
            "FROM v01_model_promotions_v3"
        )
        conn.execute("DROP TABLE v01_model_promotions_v3")
        conn.execute(
            "DELETE FROM v01_model_settings_schema WHERE version = 3"
        )

    reopened = V01ModelSettings(SQLiteStore(store.path))
    legacy = reopened.promotion_receipt(result.evidence_sha256)
    assert legacy is not None
    assert legacy.activation_request_sha256 is None
    assert legacy.activation_attestation_sha256 is None
    assert reopened.snapshot()["model"] == "challenger-model"
    assert reopened.snapshot()["revision"] == 2

    port = _ChallengerPort("must-not-run")
    with pytest.raises(
        TrainingModelActivationError,
        match="predates fresh loaded-model attestation",
    ):
        await activate_attested_training_promotion(
            result=result,
            settings=reopened,
            expected_revision=2,
            effect_port=port,
        )
    assert port.calls == 0

    rolled_back = rollback_attested_training_promotion(
        result=result,
        settings=reopened,
        expected_revision=2,
    )
    assert rolled_back.rollback_revision == 3
    assert reopened.snapshot()["model"] == "base-model"
    assert reopened.snapshot()["revision"] == 3


def test_v1_settings_database_migrates_without_losing_route(tmp_path) -> None:
    store, settings = _configured_model_settings(tmp_path)
    before = settings.snapshot()
    with store.connection() as conn:
        conn.execute("DROP TABLE v01_model_promotions")
        conn.execute(
            "DELETE FROM v01_model_settings_schema WHERE version >= 2"
        )

    reopened = V01ModelSettings(SQLiteStore(store.path))

    assert reopened.snapshot() == before
    with store.connection() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM v01_model_settings_schema WHERE version IN (2, 3)"
        ).fetchone()[0] == 2
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name = 'v01_model_promotions'"
        ).fetchone()[0] == 1


@pytest.mark.asyncio
async def test_corrupt_promotion_receipt_fails_closed_without_route_mutation(
    tmp_path,
) -> None:
    result = await _promoted_comparison(tmp_path)
    store, settings = _configured_model_settings(tmp_path)
    receipt = await activate_attested_training_promotion(
        result=result,
        settings=settings,
        expected_revision=1,
        effect_port=_ChallengerPort("activation-ok"),
    )
    with store.connection() as conn:
        conn.execute(
            "UPDATE v01_model_promotions SET challenger_artifact_sha256 = ? "
            "WHERE decision_sha256 = ?",
            (_sha(b"substituted-challenger-artifact"), receipt.decision_sha256),
        )

    with pytest.raises(
        TrainingModelActivationError,
        match="rollback was rejected",
    ):
        rollback_attested_training_promotion(
            result=result,
            settings=settings,
            expected_revision=2,
        )

    assert settings.snapshot()["model"] == "challenger-model"
    assert settings.snapshot()["revision"] == 2


def test_settings_reject_foundry_automatic_promotion_without_weight_pin(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "foundry-promotion.db")
    store.initialize()
    settings = V01ModelSettings(store)
    configured = settings.configure(
        {
            "route_kind": "foundry_local",
            "provider_id": "foundry-local",
            "model": "base-model",
            "base_url": None,
            "credential_ref": None,
            "private_data_allowed": False,
            "timeout_seconds": 30,
            "revision": 0,
        }
    )
    assert configured.status == "completed"

    with pytest.raises(ModelSetupError, match="Ollama"):
        settings.activate_promoted_local_model(
            expected_revision=1,
            base_provider_id="foundry-local",
            base_model_id="base-model",
            challenger_provider_id="foundry-local",
            challenger_model_id="challenger-model",
            decision_sha256=_sha(b"foundry-decision"),
            binding_sha256=_sha(b"foundry-binding"),
            base_artifact_sha256=_sha(b"foundry-base-artifact"),
            base_descriptor_digest=_sha(b"foundry-base-descriptor"),
            challenger_artifact_sha256=_sha(b"foundry-challenger-artifact"),
            challenger_descriptor_digest=_sha(b"foundry-challenger-descriptor"),
            activation_request_sha256=_sha(b"foundry-activation-request"),
            activation_attestation_sha256=_sha(b"foundry-activation-attestation"),
        )

    assert settings.snapshot()["model"] == "base-model"
    assert settings.snapshot()["revision"] == 1
