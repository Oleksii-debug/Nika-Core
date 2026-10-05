from __future__ import annotations

import hashlib
import json
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
from nika_core.model_engineering import (
    BenchmarkExecutionConfig,
    EvaluationCase,
    EvaluationPurpose,
    EvaluationSet,
    ModelCandidate,
)
from nika_core.model_engineering.experiment_bridge import (
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
from nika_core.training_evaluation_attestation import (
    AttestedModelCompletionResult,
    LoadedModelArtifactAttestation,
)
from nika_core.training_evaluation_binding import TrainingEvaluationBinding
from nika_core.training_evaluation_champion_execution import (
    AttestedChampionBenchmarkResult,
    run_attested_champion_benchmark,
)
from nika_core.training_evaluation_execution import (
    AttestedChallengerBenchmarkResult,
    run_attested_challenger_benchmark,
)
from nika_core.training_evaluation_promotion import (
    AttestedOldVsNewDecision,
    AttestedOldVsNewDecisionError,
    evaluate_attested_old_vs_new,
)


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


_ATTESTOR_ID = "shared-attestor"
_ATTESTOR_SHA256 = _sha(b"shared-attestor-binary")


def _evaluation() -> EvaluationSet:
    return EvaluationSet(
        evaluation_set_id="promotion-held-out",
        version="v1",
        provenance_ref="dataset:promotion-held-out",
        license_ref="license:promotion-held-out",
        purpose=EvaluationPurpose.HELD_OUT,
        privacy=PrivacyClass.PRIVATE,
        cases=(
            EvaluationCase(
                case_id="case-one",
                messages=(ModelMessage("user", "secret old-new prompt one"),),
                expected_text="one",
            ),
            EvaluationCase(
                case_id="case-two",
                messages=(ModelMessage("user", "secret old-new prompt two"),),
                expected_text="two",
            ),
        ),
    )


def _candidates(
    evaluation: EvaluationSet,
) -> tuple[TrainingEvaluationBinding, ModelCandidate, ModelCandidate]:
    champion_sha = _sha(b"champion-weights")
    challenger_sha = _sha(b"challenger-weights")
    champion = ModelCandidate(
        candidate_id="models/base",
        provider_id="ollama",
        provider_kind=ProviderKind.LOCAL,
        request_model="champion-model",
        expected_response_model="champion-model",
        engine_provenance_ref="engine:ollama",
        engine_license_ref="license:ollama",
        model_provenance_ref="model:champion-v1",
        model_license_ref="license:champion-v1",
        model_sha256=champion_sha,
    )
    challenger = ModelCandidate(
        candidate_id="models/candidate/job-1",
        provider_id="ollama",
        provider_kind=ProviderKind.LOCAL,
        request_model="challenger-model",
        expected_response_model="challenger-model",
        engine_provenance_ref="engine:ollama",
        engine_license_ref="license:ollama",
        model_provenance_ref="model:challenger-job-1",
        model_license_ref="license:challenger-job-1",
        model_sha256=challenger_sha,
    )
    binding = TrainingEvaluationBinding(
        job_id="job-1",
        base_candidate_id=champion.candidate_id,
        base_provider_id=champion.provider_id,
        base_model_id=champion.request_model,
        challenger_candidate_id=challenger.candidate_id,
        challenger_provider_id=challenger.provider_id,
        challenger_model_id=challenger.request_model,
        base_sha256=champion_sha,
        challenger_sha256=challenger_sha,
        candidate_artifact_ref=challenger.candidate_id,
        frozen_package_sha256=_sha(b"frozen-package"),
        evaluation_set_sha256=evaluation.content_sha256,
        base_descriptor_digest=_sha(b"base-descriptor"),
        base_descriptor_registry_key=_sha(b"base-registry"),
        base_size_bytes=len(b"champion-weights"),
        descriptor_digest=_sha(b"challenger-descriptor"),
        descriptor_registry_key=_sha(b"challenger-registry"),
        challenger_size_bytes=len(b"challenger-weights"),
    )
    return binding, champion, challenger


class _Effect:
    def __init__(
        self,
        answers: dict[str, str],
        *,
        attestor_id: str = _ATTESTOR_ID,
        attestor_sha256: str = _ATTESTOR_SHA256,
    ) -> None:
        self.answers = answers
        self.attestor_id = attestor_id
        self.attestor_sha256 = attestor_sha256
        self.calls: list[str] = []

    async def complete_attested(
        self,
        request,
        *,
        binding,
    ) -> AttestedModelCompletionResult:
        case_id = request.metadata["evaluation_case_id"]
        self.calls.append(case_id)
        return AttestedModelCompletionResult(
            response=ModelResponse(
                request_id=request.request_id,
                text=self.answers[case_id],
                provider_id=binding.challenger_provider_id,
                provider_kind=ProviderKind.LOCAL,
                model=binding.challenger_model_id,
                usage=ModelUsage(
                    input_tokens=2,
                    output_tokens=1,
                    total_tokens=3,
                ),
            ),
            attestation=LoadedModelArtifactAttestation(
                request_id=request.request_id,
                binding_sha256=binding.binding_sha256,
                provider_id=binding.challenger_provider_id,
                model_id=binding.challenger_model_id,
                artifact_sha256=binding.challenger_sha256,
                descriptor_digest=binding.descriptor_digest,
                attestor_id=self.attestor_id,
                attestor_sha256=self.attestor_sha256,
            ),
        )


async def _benchmark_pair(
    *,
    champion_answers: dict[str, str],
    challenger_answers: dict[str, str],
    challenger_attestor_id: str = _ATTESTOR_ID,
    challenger_attestor_sha256: str = _ATTESTOR_SHA256,
) -> tuple[
    AttestedChampionBenchmarkResult,
    AttestedChallengerBenchmarkResult,
    EvaluationSet,
]:
    evaluation = _evaluation()
    binding, champion, challenger = _candidates(evaluation)
    champion_result = await run_attested_champion_benchmark(
        binding=binding,
        champion=champion,
        evaluation_set=evaluation,
        effect_port=_Effect(champion_answers),
        expected_attestor_id=_ATTESTOR_ID,
        expected_attestor_sha256=_ATTESTOR_SHA256,
    )
    challenger_result = await run_attested_challenger_benchmark(
        binding=binding,
        challenger=challenger,
        evaluation_set=evaluation,
        effect_port=_Effect(
            challenger_answers,
            attestor_id=challenger_attestor_id,
            attestor_sha256=challenger_attestor_sha256,
        ),
        expected_attestor_id=challenger_attestor_id,
        expected_attestor_sha256=challenger_attestor_sha256,
    )
    return champion_result, challenger_result, evaluation


class _TrackingRepository(InMemoryExperimentRepository):
    def __init__(self) -> None:
        super().__init__()
        self.create_calls = 0

    def create(self, snapshot) -> None:
        self.create_calls += 1
        super().create(snapshot)


@pytest.mark.asyncio
async def test_attested_challenger_win_is_decided_by_existing_engine() -> None:
    champion, challenger, evaluation = await _benchmark_pair(
        champion_answers={"case-one": "one", "case-two": "wrong"},
        challenger_answers={"case-one": "one", "case-two": "two"},
    )

    decision = evaluate_attested_old_vs_new(
        experiment_id="loop-c-job-1",
        champion_benchmark=champion,
        challenger_benchmark=challenger,
        evaluation_set=evaluation,
        execution_config=BenchmarkExecutionConfig(),
        policy=PromotionPolicy(
            primary_metric=QUALITY_METRIC,
            minimum_improvement=0.1,
            minimum_replays=2,
        ),
        permission_fingerprint="permissions:v1",
        repository=InMemoryExperimentRepository(),
    )

    assert decision.snapshot.status is ExperimentStatus.PROMOTED
    assert (
        decision.snapshot.selected_candidate_id
        == challenger.report.candidate.candidate_id
    )
    assert (
        decision.snapshot.previous_champion_id
        == champion.report.candidate.candidate_id
    )
    assert decision.evidence_payload()["model_activation_performed"] is False


@pytest.mark.asyncio
async def test_insufficient_improvement_keeps_champion() -> None:
    champion, challenger, evaluation = await _benchmark_pair(
        champion_answers={"case-one": "one", "case-two": "wrong"},
        challenger_answers={"case-one": "one", "case-two": "two"},
    )

    decision = evaluate_attested_old_vs_new(
        experiment_id="loop-c-high-threshold",
        champion_benchmark=champion,
        challenger_benchmark=challenger,
        evaluation_set=evaluation,
        execution_config=BenchmarkExecutionConfig(),
        policy=PromotionPolicy(
            primary_metric=QUALITY_METRIC,
            minimum_improvement=0.75,
            minimum_replays=2,
        ),
        permission_fingerprint="permissions:v1",
        repository=InMemoryExperimentRepository(),
    )

    assert decision.snapshot.status is ExperimentStatus.COMPLETED
    assert (
        decision.snapshot.selected_candidate_id
        == champion.report.candidate.candidate_id
    )
    assert (
        decision.snapshot.previous_champion_id
        == champion.report.candidate.candidate_id
    )


@pytest.mark.asyncio
async def test_mismatched_attestor_fails_before_repository_mutation() -> None:
    champion, challenger, evaluation = await _benchmark_pair(
        champion_answers={"case-one": "one", "case-two": "wrong"},
        challenger_answers={"case-one": "one", "case-two": "two"},
        challenger_attestor_id="other-attestor",
        challenger_attestor_sha256=_sha(b"other-attestor"),
    )
    repository = _TrackingRepository()

    with pytest.raises(
        AttestedOldVsNewDecisionError,
        match="same attestor authority",
    ):
        evaluate_attested_old_vs_new(
            experiment_id="loop-c-attestor-substitution",
            champion_benchmark=champion,
            challenger_benchmark=challenger,
            evaluation_set=evaluation,
            execution_config=BenchmarkExecutionConfig(),
            policy=PromotionPolicy(
                primary_metric=QUALITY_METRIC,
                minimum_replays=2,
            ),
            permission_fingerprint="permissions:v1",
            repository=repository,
        )

    assert repository.create_calls == 0


@pytest.mark.asyncio
async def test_execution_config_substitution_fails_before_repository_mutation() -> None:
    champion, challenger, evaluation = await _benchmark_pair(
        champion_answers={"case-one": "one", "case-two": "wrong"},
        challenger_answers={"case-one": "one", "case-two": "two"},
    )
    repository = _TrackingRepository()

    with pytest.raises(
        AttestedOldVsNewDecisionError,
        match="execution configuration",
    ):
        evaluate_attested_old_vs_new(
            experiment_id="loop-c-config-substitution",
            champion_benchmark=champion,
            challenger_benchmark=challenger,
            evaluation_set=evaluation,
            execution_config=BenchmarkExecutionConfig(timeout_seconds=30.0),
            policy=PromotionPolicy(
                primary_metric=QUALITY_METRIC,
                minimum_replays=2,
            ),
            permission_fingerprint="permissions:v1",
            repository=repository,
        )

    assert repository.create_calls == 0


@pytest.mark.asyncio
async def test_evaluation_substitution_fails_before_repository_mutation() -> None:
    champion, challenger, evaluation = await _benchmark_pair(
        champion_answers={"case-one": "one", "case-two": "wrong"},
        challenger_answers={"case-one": "one", "case-two": "two"},
    )
    repository = _TrackingRepository()
    substituted = replace(evaluation, version="other")

    with pytest.raises(
        AttestedOldVsNewDecisionError,
        match="evaluation set",
    ):
        evaluate_attested_old_vs_new(
            experiment_id="loop-c-evaluation-substitution",
            champion_benchmark=champion,
            challenger_benchmark=challenger,
            evaluation_set=substituted,
            execution_config=BenchmarkExecutionConfig(),
            policy=PromotionPolicy(
                primary_metric=QUALITY_METRIC,
                minimum_replays=2,
            ),
            permission_fingerprint="permissions:v1",
            repository=repository,
        )

    assert repository.create_calls == 0


@pytest.mark.asyncio
async def test_tampered_benchmark_receipt_fails_before_repository_mutation() -> None:
    champion, challenger, evaluation = await _benchmark_pair(
        champion_answers={"case-one": "one", "case-two": "wrong"},
        challenger_answers={"case-one": "one", "case-two": "two"},
    )
    repository = _TrackingRepository()
    forged = replace(
        challenger.case_receipts[0],
        artifact_sha256=_sha(b"forged-artifact"),
    )
    object.__setattr__(
        challenger,
        "case_receipts",
        (forged, challenger.case_receipts[1]),
    )

    with pytest.raises(
        AttestedOldVsNewDecisionError,
        match="benchmark evidence",
    ):
        evaluate_attested_old_vs_new(
            experiment_id="loop-c-receipt-substitution",
            champion_benchmark=champion,
            challenger_benchmark=challenger,
            evaluation_set=evaluation,
            execution_config=BenchmarkExecutionConfig(),
            policy=PromotionPolicy(
                primary_metric=QUALITY_METRIC,
                minimum_replays=2,
            ),
            permission_fingerprint="permissions:v1",
            repository=repository,
        )

    assert repository.create_calls == 0


@pytest.mark.asyncio
async def test_decision_evidence_is_digest_only_for_held_out_content() -> None:
    champion, challenger, evaluation = await _benchmark_pair(
        champion_answers={"case-one": "one", "case-two": "wrong"},
        challenger_answers={"case-one": "one", "case-two": "two"},
    )
    decision = evaluate_attested_old_vs_new(
        experiment_id="loop-c-secret-minimized",
        champion_benchmark=champion,
        challenger_benchmark=challenger,
        evaluation_set=evaluation,
        execution_config=BenchmarkExecutionConfig(),
        policy=PromotionPolicy(
            primary_metric=QUALITY_METRIC,
            minimum_improvement=0.1,
            minimum_replays=2,
        ),
        permission_fingerprint="permissions:v1",
        repository=InMemoryExperimentRepository(),
    )

    payload = decision.evidence_payload()
    body = json.dumps(payload, ensure_ascii=False)

    assert payload["schema"] == "nika-attested-old-vs-new-decision-v1"
    assert payload["evaluation_set_sha256"] == evaluation.content_sha256
    assert payload["observation_count"] == 4
    assert payload["model_activation_performed"] is False
    assert "secret old-new prompt one" not in body
    assert "secret old-new prompt two" not in body
    assert '"wrong"' not in body
    assert "permissions:v1" not in body


@pytest.mark.asyncio
async def test_decision_revalidation_rejects_terminal_snapshot_mutation() -> None:
    champion, challenger, evaluation = await _benchmark_pair(
        champion_answers={"case-one": "one", "case-two": "wrong"},
        challenger_answers={"case-one": "one", "case-two": "two"},
    )
    decision = evaluate_attested_old_vs_new(
        experiment_id="loop-c-terminal-mutation",
        champion_benchmark=champion,
        challenger_benchmark=challenger,
        evaluation_set=evaluation,
        execution_config=BenchmarkExecutionConfig(),
        policy=PromotionPolicy(
            primary_metric=QUALITY_METRIC,
            minimum_improvement=0.1,
            minimum_replays=2,
        ),
        permission_fingerprint="permissions:v1",
        repository=InMemoryExperimentRepository(),
    )
    object.__setattr__(
        decision.snapshot,
        "selected_candidate_id",
        champion.report.candidate.candidate_id,
    )

    with pytest.raises(ValueError, match="terminal decision"):
        decision.evidence_payload()


def test_direct_attested_old_new_decision_construction_is_disabled() -> None:
    with pytest.raises(TypeError):
        AttestedOldVsNewDecision(
            snapshot=None,
            champion_benchmark=None,
            challenger_benchmark=None,
            evaluation_set=None,
            execution_config=None,
            permission_fingerprint="permissions:v1",
        )

def _promotion_policy() -> PromotionPolicy:
    return PromotionPolicy(
        primary_metric=QUALITY_METRIC,
        minimum_improvement=0.1,
        minimum_replays=2,
    )


async def _promotion_pair():
    return await _benchmark_pair(
        champion_answers={"case-one": "one", "case-two": "wrong"},
        challenger_answers={"case-one": "one", "case-two": "two"},
    )


def _definition_and_observations(
    *,
    experiment_id: str,
    champion: AttestedChampionBenchmarkResult,
    challenger: AttestedChallengerBenchmarkResult,
    evaluation: EvaluationSet,
    config: BenchmarkExecutionConfig,
    policy: PromotionPolicy,
    permission_fingerprint: str,
):
    definition = build_experiment_definition(
        experiment_id=experiment_id,
        champion=champion.report.candidate,
        challengers=(challenger.report.candidate,),
        evaluation_set=evaluation,
        execution_config=config,
        policy=policy,
        permission_fingerprint=permission_fingerprint,
    )
    observations = (
        *benchmark_observations(
            champion.report,
            definition=definition,
            evaluation_set=evaluation,
        ),
        *benchmark_observations(
            challenger.report,
            definition=definition,
            evaluation_set=evaluation,
        ),
    )
    return definition, observations


@pytest.mark.asyncio
async def test_terminal_decision_is_idempotent_on_same_repository() -> None:
    champion, challenger, evaluation = await _promotion_pair()
    repository = _TrackingRepository()
    config = BenchmarkExecutionConfig()
    policy = _promotion_policy()
    kwargs = dict(
        experiment_id="loop-c-idempotent",
        champion_benchmark=champion,
        challenger_benchmark=challenger,
        evaluation_set=evaluation,
        execution_config=config,
        policy=policy,
        permission_fingerprint="permissions:v1",
        repository=repository,
    )

    first = evaluate_attested_old_vs_new(**kwargs)
    second = evaluate_attested_old_vs_new(**kwargs)

    assert repository.create_calls == 1
    assert first.snapshot == second.snapshot
    assert first.evidence_sha256 == second.evidence_sha256
    assert second.snapshot.status is ExperimentStatus.PROMOTED
    assert len(second.snapshot.observations) == 4


@pytest.mark.asyncio
async def test_partial_experiment_resumes_without_duplicate_observations() -> None:
    champion, challenger, evaluation = await _promotion_pair()
    repository = InMemoryExperimentRepository()
    config = BenchmarkExecutionConfig()
    policy = _promotion_policy()
    definition, observations = _definition_and_observations(
        experiment_id="loop-c-partial-resume",
        champion=champion,
        challenger=challenger,
        evaluation=evaluation,
        config=config,
        policy=policy,
        permission_fingerprint="permissions:v1",
    )
    engine = ExperimentEngine(repository)
    engine.create(definition)
    engine.start(definition.experiment_id)
    engine.record(definition.experiment_id, observations[0])

    decision = evaluate_attested_old_vs_new(
        experiment_id=definition.experiment_id,
        champion_benchmark=champion,
        challenger_benchmark=challenger,
        evaluation_set=evaluation,
        execution_config=config,
        policy=policy,
        permission_fingerprint="permissions:v1",
        repository=repository,
    )

    assert decision.snapshot.status is ExperimentStatus.PROMOTED
    assert len(decision.snapshot.observations) == 4
    keys = {
        (item.candidate_id, item.replay_id, item.metric)
        for item in decision.snapshot.observations
    }
    assert len(keys) == 4


@pytest.mark.asyncio
async def test_conflicting_partial_observation_fails_closed() -> None:
    champion, challenger, evaluation = await _promotion_pair()
    repository = InMemoryExperimentRepository()
    config = BenchmarkExecutionConfig()
    policy = _promotion_policy()
    definition, observations = _definition_and_observations(
        experiment_id="loop-c-conflicting-partial",
        champion=champion,
        challenger=challenger,
        evaluation=evaluation,
        config=config,
        policy=policy,
        permission_fingerprint="permissions:v1",
    )
    conflicting = replace(
        observations[0],
        value=float(observations[0].value) + 0.25,
    )
    engine = ExperimentEngine(repository)
    engine.create(definition)
    engine.start(definition.experiment_id)
    engine.record(definition.experiment_id, conflicting)

    with pytest.raises(
        AttestedOldVsNewDecisionError,
        match="conflicts with attested benchmark",
    ):
        evaluate_attested_old_vs_new(
            experiment_id=definition.experiment_id,
            champion_benchmark=champion,
            challenger_benchmark=challenger,
            evaluation_set=evaluation,
            execution_config=config,
            policy=policy,
            permission_fingerprint="permissions:v1",
            repository=repository,
        )

    persisted = repository.get(definition.experiment_id)
    assert persisted.observations == (conflicting,)


@pytest.mark.asyncio
async def test_sqlite_restart_resumes_partial_decision(tmp_path) -> None:
    champion, challenger, evaluation = await _promotion_pair()
    config = BenchmarkExecutionConfig()
    policy = _promotion_policy()
    definition, observations = _definition_and_observations(
        experiment_id="loop-c-sqlite-restart",
        champion=champion,
        challenger=challenger,
        evaluation=evaluation,
        config=config,
        policy=policy,
        permission_fingerprint="permissions:v1",
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
    decision = evaluate_attested_old_vs_new(
        experiment_id=definition.experiment_id,
        champion_benchmark=champion,
        challenger_benchmark=challenger,
        evaluation_set=evaluation,
        execution_config=config,
        policy=policy,
        permission_fingerprint="permissions:v1",
        repository=reopened_repository,
    )

    assert decision.snapshot.status is ExperimentStatus.PROMOTED
    assert len(decision.snapshot.observations) == 4
    assert reopened_repository.get(definition.experiment_id) == decision.snapshot


@pytest.mark.asyncio
async def test_existing_conflicting_definition_fails_before_mutation() -> None:
    champion, challenger, evaluation = await _promotion_pair()
    repository = InMemoryExperimentRepository()
    config = BenchmarkExecutionConfig()
    policy = _promotion_policy()
    conflicting_definition, _ = _definition_and_observations(
        experiment_id="loop-c-definition-conflict",
        champion=champion,
        challenger=challenger,
        evaluation=evaluation,
        config=config,
        policy=policy,
        permission_fingerprint="permissions:other",
    )
    ExperimentEngine(repository).create(conflicting_definition)

    with pytest.raises(
        AttestedOldVsNewDecisionError,
        match="definition conflicts",
    ):
        evaluate_attested_old_vs_new(
            experiment_id=conflicting_definition.experiment_id,
            champion_benchmark=champion,
            challenger_benchmark=challenger,
            evaluation_set=evaluation,
            execution_config=config,
            policy=policy,
            permission_fingerprint="permissions:v1",
            repository=repository,
        )

    persisted = repository.get(conflicting_definition.experiment_id)
    assert persisted.status is ExperimentStatus.DRAFT
    assert persisted.observations == ()


@pytest.mark.asyncio
async def test_rolled_back_decision_cannot_be_reused_as_fresh_evidence() -> None:
    champion, challenger, evaluation = await _promotion_pair()
    repository = InMemoryExperimentRepository()
    config = BenchmarkExecutionConfig()
    policy = _promotion_policy()
    experiment_id = "loop-c-rolled-back"
    first = evaluate_attested_old_vs_new(
        experiment_id=experiment_id,
        champion_benchmark=champion,
        challenger_benchmark=challenger,
        evaluation_set=evaluation,
        execution_config=config,
        policy=policy,
        permission_fingerprint="permissions:v1",
        repository=repository,
    )
    assert first.snapshot.status is ExperimentStatus.PROMOTED
    ExperimentEngine(repository).rollback(experiment_id)

    with pytest.raises(
        AttestedOldVsNewDecisionError,
        match="rolled-back experiment",
    ):
        evaluate_attested_old_vs_new(
            experiment_id=experiment_id,
            champion_benchmark=champion,
            challenger_benchmark=challenger,
            evaluation_set=evaluation,
            execution_config=config,
            policy=policy,
            permission_fingerprint="permissions:v1",
            repository=repository,
        )

