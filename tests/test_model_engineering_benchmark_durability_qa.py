"""QA_ONLY / DO_NOT_MERGE oracle for Model Engineering benchmark durability.

Production target: PR #534 exact head c91d95e076ae7593c39d22c6e0ebf937b139773e.
This file intentionally contains one expected-RED contract test proving that the
current benchmark evidence identity does not bind execution configuration.
Production repair belongs to the incumbent #507/#534 owner.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.experiments.contracts import ExperimentStatus, PromotionPolicy
from nika_core.experiments.engine import ExperimentEngine
from nika_core.experiments.repository import SQLiteExperimentRepository
from nika_core.model_engineering import (
    QUALITY_METRIC,
    CandidateBenchmarkReport,
    CaseBenchmarkResult,
    EvaluationCase,
    EvaluationPurpose,
    EvaluationSet,
    ModelBenchmarkRunner,
    ModelCandidate,
    benchmark_observations,
    benchmark_report_json,
    build_experiment_definition,
)
from nika_core.model_gateway.contracts import (
    ModelMessage,
    ModelResponse,
    ModelUsage,
    PrivacyClass,
    ProviderKind,
)


def _candidate(candidate_id: str, model: str) -> ModelCandidate:
    return ModelCandidate(
        candidate_id=candidate_id,
        provider_id="local-provider",
        provider_kind=ProviderKind.LOCAL,
        request_model=model,
        expected_response_model=model,
        engine_provenance_ref="engine:local-provider@1",
        engine_license_ref="license:engine",
        model_provenance_ref=f"model:{model}@pinned",
        model_license_ref="license:model",
    )


def _evaluation(version: str = "v1") -> EvaluationSet:
    return EvaluationSet(
        evaluation_set_id="durability-held-out",
        version=version,
        provenance_ref="dataset:durability-held-out",
        license_ref="license:eval",
        purpose=EvaluationPurpose.HELD_OUT,
        privacy=PrivacyClass.PUBLIC,
        cases=(
            EvaluationCase(
                case_id="case-1",
                messages=(ModelMessage("user", "one"),),
                expected_text="one",
            ),
            EvaluationCase(
                case_id="case-2",
                messages=(ModelMessage("user", "two"),),
                expected_text="two",
            ),
        ),
    )


def _definition(experiment_id: str = "benchmark-run-1"):
    champion = _candidate("champion", "model-a")
    challenger = _candidate("challenger", "model-b")
    evaluation = _evaluation()
    definition = build_experiment_definition(
        experiment_id=experiment_id,
        champion=champion,
        challengers=(challenger,),
        evaluation_set=evaluation,
        policy=PromotionPolicy(
            primary_metric=QUALITY_METRIC,
            minimum_improvement=0.1,
            minimum_replays=2,
        ),
        permission_fingerprint="permissions-v1",
    )
    return champion, challenger, evaluation, definition


def _report(
    candidate: ModelCandidate,
    evaluation: EvaluationSet,
    scores: tuple[float, float],
) -> CandidateBenchmarkReport:
    case_results = tuple(
        CaseBenchmarkResult(
            candidate_id=candidate.candidate_id,
            case_id=case.case_id,
            score=score,
            passed=score >= case.pass_score,
            completion_succeeded=True,
            latency_ms=10.0 + index,
            response_sha256=("a" if candidate.candidate_id == "champion" else "b") * 64,
            error_code=None,
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            resource_before=None,
            resource_after=None,
            accelerator_before=None,
            accelerator_after=None,
        )
        for index, (case, score) in enumerate(zip(evaluation.cases, scores, strict=True))
    )
    return CandidateBenchmarkReport(
        candidate=candidate,
        evaluation_set_id=evaluation.evaluation_set_id,
        evaluation_set_version=evaluation.version,
        evaluation_set_sha256=evaluation.content_sha256,
        evaluation_purpose=evaluation.purpose,
        case_results=case_results,
        weighted_quality_score=sum(scores) / len(scores),
        task_pass_rate=sum(item.passed for item in case_results) / len(case_results),
        completion_rate=1.0,
        mean_latency_ms=sum(item.latency_ms for item in case_results) / len(case_results),
        p95_latency_ms=max(item.latency_ms for item in case_results),
        peak_cpu_percent=None,
        peak_memory_percent=None,
        min_available_memory_bytes=None,
        peak_accelerator_percent=None,
        peak_accelerator_memory_bytes=None,
    )


def _fresh_engine(path):
    store = SQLiteStore(path)
    store.initialize()
    repository = SQLiteExperimentRepository(store)
    return repository, ExperimentEngine(repository)


def _record_report(engine, definition, evaluation, report) -> None:
    for observation in benchmark_observations(
        report,
        definition=definition,
        evaluation_set=evaluation,
    ):
        engine.record(definition.experiment_id, observation)


def test_running_partial_restart_stays_running_and_cannot_complete(tmp_path) -> None:
    database = tmp_path / "nika.db"
    champion, _, evaluation, definition = _definition()
    _, engine = _fresh_engine(database)
    engine.create(definition)
    engine.start(definition.experiment_id)

    first = benchmark_observations(
        _report(champion, evaluation, (1.0, 1.0)),
        definition=definition,
        evaluation_set=evaluation,
    )[0]
    engine.record(definition.experiment_id, first)

    repository_after_restart, engine_after_restart = _fresh_engine(database)
    recovered = repository_after_restart.get(definition.experiment_id)
    assert recovered.status is ExperimentStatus.RUNNING
    assert recovered.observations == (first,)

    with pytest.raises(ValueError, match="incomplete replay coverage"):
        engine_after_restart.complete(definition.experiment_id)

    repository_after_second_restart, _ = _fresh_engine(database)
    still_running = repository_after_second_restart.get(definition.experiment_id)
    assert still_running.status is ExperimentStatus.RUNNING
    assert still_running.observations == (first,)


def test_duplicate_run_id_cannot_rebind_dataset_or_candidate_identity(tmp_path) -> None:
    database = tmp_path / "nika.db"
    _, _, _, definition = _definition("duplicate-run")
    repository, engine = _fresh_engine(database)
    engine.create(definition)

    different = replace(
        definition,
        champion=replace(definition.champion, version="sha256:" + "0" * 64),
    )
    with pytest.raises(ValueError, match="experiment already exists"):
        engine.create(different)

    repository_after_restart, _ = _fresh_engine(database)
    recovered = repository_after_restart.get(definition.experiment_id)
    assert recovered.definition == definition
    assert recovered.status is ExperimentStatus.DRAFT
    assert repository.get(definition.experiment_id).definition == definition


def test_terminal_commit_rolls_back_atomically_then_survives_restart(
    tmp_path, monkeypatch
) -> None:
    database = tmp_path / "nika.db"
    champion, challenger, evaluation, definition = _definition("atomic-final")
    repository, engine = _fresh_engine(database)
    engine.create(definition)
    engine.start(definition.experiment_id)
    _record_report(engine, definition, evaluation, _report(champion, evaluation, (1.0, 1.0)))
    _record_report(engine, definition, evaluation, _report(challenger, evaluation, (0.0, 0.0)))
    running = repository.get(definition.experiment_id)
    assert running.status is ExperimentStatus.RUNNING
    expected_observations = running.observations

    def _fail_event(*_args, **_kwargs):
        raise RuntimeError("synthetic crash before transaction commit")

    with monkeypatch.context() as patch:
        patch.setattr(
            SQLiteExperimentRepository,
            "_append_event",
            staticmethod(_fail_event),
        )
        with pytest.raises(RuntimeError, match="synthetic crash"):
            engine.complete(definition.experiment_id)

    repository_after_crash, engine_after_crash = _fresh_engine(database)
    recovered_running = repository_after_crash.get(definition.experiment_id)
    assert recovered_running.status is ExperimentStatus.RUNNING
    assert recovered_running.selected_candidate_id is None
    assert recovered_running.previous_champion_id is None
    assert recovered_running.observations == expected_observations

    completed = engine_after_crash.complete(definition.experiment_id)
    assert completed.status is ExperimentStatus.COMPLETED
    assert completed.selected_candidate_id == champion.candidate_id

    repository_after_restart, _ = _fresh_engine(database)
    durable = repository_after_restart.get(definition.experiment_id)
    assert durable == completed
    assert durable.definition.replays == definition.replays
    assert durable.definition.champion == definition.champion
    assert durable.definition.challengers == definition.challengers


class _CaptureGateway:
    def __init__(self) -> None:
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        expected = "one" if request.metadata["evaluation_case_id"] == "case-1" else "two"
        return ModelResponse(
            request_id=request.request_id,
            text=expected,
            provider_id=request.provider_id,
            provider_kind=ProviderKind.LOCAL,
            model=request.model,
            usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
        )


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        self.value += 0.01
        return self.value


def test_completed_evidence_identity_binds_execution_config__expected_red() -> None:
    """RED on #534: timeout/temperature changes currently reuse evidence identity."""

    candidate = _candidate("candidate", "model-a")
    evaluation = _evaluation()
    gateway = _CaptureGateway()
    runner = ModelBenchmarkRunner(gateway, clock=_Clock())

    first = asyncio.run(
        runner.benchmark(
            candidate,
            evaluation,
            timeout_seconds=10.0,
            temperature=0.0,
        )
    )
    second = asyncio.run(
        runner.benchmark(
            candidate,
            evaluation,
            timeout_seconds=30.0,
            temperature=0.7,
        )
    )

    first_ids = tuple(request.request_id for request in gateway.requests[:2])
    second_ids = tuple(request.request_id for request in gateway.requests[2:])
    assert first_ids != second_ids
    assert benchmark_report_json(first) != benchmark_report_json(second)
