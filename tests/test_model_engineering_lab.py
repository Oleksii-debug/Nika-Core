from __future__ import annotations

from dataclasses import replace

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.experiments.contracts import MetricObservation
from nika_core.experiments.engine import ExperimentEngine
from nika_core.experiments.repository import (
    InMemoryExperimentRepository,
    SQLiteExperimentRepository,
)
from nika_core.model_engineering import (
    CaseMeasurement,
    EvaluationCase,
    MetricDefinition,
    MetricValue,
    ModelCandidate,
    ModelEngineeringLab,
    ModelExperimentSpec,
)
from nika_core.model_gateway.contracts import PrivacyClass, ProviderKind


def _candidate(candidate_id: str, *, model_version: str) -> ModelCandidate:
    return ModelCandidate(
        candidate_id=candidate_id,
        provider_id=f"provider-{candidate_id}",
        provider_kind=ProviderKind.LOCAL,
        model_id=f"model-{candidate_id}",
        model_version=model_version,
        source_ref=f"local-model-registry://{candidate_id}/{model_version}",
        license_ref="license-evidence://approved-model-license",
        permission_fingerprint="perm:v1:private-local-only",
        supports_private_data=True,
        artifact_sha256="a" * 64 if candidate_id == "champion" else "b" * 64,
    )


def _case(case_id: str, *, dataset_version: str) -> EvaluationCase:
    return EvaluationCase(
        case_id=case_id,
        dataset_ref="corpus://eval/model-routing-heldout",
        dataset_version=dataset_version,
        dataset_sha256="c" * 64,
        privacy=PrivacyClass.PRIVATE,
    )


def _spec() -> ModelExperimentSpec:
    return ModelExperimentSpec(
        experiment_id="model-exp-001",
        champion=_candidate("champion", model_version="1.0.0"),
        challengers=(_candidate("challenger", model_version="2.0.0"),),
        cases=(
            _case("case-1", dataset_version="2026-08-26.1"),
            _case("case-2", dataset_version="2026-08-26.1"),
        ),
        primary_metric=MetricDefinition("quality", higher_is_better=True),
        guardrails=(
            MetricDefinition("latency_ms", higher_is_better=False, max_regression=25.0),
        ),
        minimum_improvement=0.05,
    )


def _measurement(
    candidate_id: str, case_id: str, *, quality: float, latency_ms: float
) -> CaseMeasurement:
    return CaseMeasurement(
        candidate_id=candidate_id,
        case_id=case_id,
        metrics=(
            MetricValue("quality", quality),
            MetricValue("latency_ms", latency_ms),
        ),
    )


def _record_complete_matrix(
    lab: ModelEngineeringLab,
    *,
    challenger_latency_ms: float,
) -> None:
    for case_id in ("case-1", "case-2"):
        lab.record_measurement(
            "model-exp-001",
            _measurement("champion", case_id, quality=0.70, latency_ms=100.0),
        )
        lab.record_measurement(
            "model-exp-001",
            _measurement(
                "challenger",
                case_id,
                quality=0.80,
                latency_ms=challenger_latency_ms,
            ),
        )


def test_model_manifests_are_bound_into_existing_experiment_definition() -> None:
    repository = InMemoryExperimentRepository()
    lab = ModelEngineeringLab(repository)

    snapshot = lab.create(_spec())

    champion = lab.decode_candidate(snapshot.definition.champion)
    case = lab.decode_case(snapshot.definition.replays[0])
    assert champion.model_id == "model-champion"
    assert champion.artifact_sha256 == "a" * 64
    assert case.dataset_version == "2026-08-26.1"
    assert case.privacy is PrivacyClass.PRIVATE
    assert snapshot.definition.champion.version != champion.model_version
    assert len(snapshot.definition.champion.version) == 64
    assert snapshot.definition.replays[0].dataset_version != case.dataset_version
    assert len(snapshot.definition.replays[0].dataset_version) == 64


def test_candidate_manifest_tamper_fails_closed() -> None:
    repository = InMemoryExperimentRepository()
    lab = ModelEngineeringLab(repository)
    snapshot = lab.create(_spec())

    tampered = replace(snapshot.definition.champion, version="0" * 64)

    with pytest.raises(ValueError, match="manifest digest mismatch"):
        lab.decode_candidate(tampered)


def test_permission_widening_is_rejected_before_experiment_creation() -> None:
    challenger = replace(
        _candidate("challenger", model_version="2.0.0"),
        permission_fingerprint="perm:v2:cloud-enabled",
    )

    with pytest.raises(PermissionError, match="may not widen or alter permissions"):
        ModelExperimentSpec(
            experiment_id="permission-mismatch",
            champion=_candidate("champion", model_version="1.0.0"),
            challengers=(challenger,),
            cases=(_case("case-1", dataset_version="1"),),
            primary_metric=MetricDefinition("quality"),
        )


def test_measurement_set_is_validated_before_any_partial_write() -> None:
    repository = InMemoryExperimentRepository()
    lab = ModelEngineeringLab(repository)
    lab.create(_spec())
    lab.start("model-exp-001")

    with pytest.raises(ValueError, match="metric set does not match"):
        lab.record_measurement(
            "model-exp-001",
            CaseMeasurement(
                candidate_id="champion",
                case_id="case-1",
                metrics=(MetricValue("quality", 0.7),),
            ),
        )

    assert repository.get("model-exp-001").observations == ()


def test_same_measurement_is_idempotent_but_conflicting_evidence_is_rejected() -> None:
    repository = InMemoryExperimentRepository()
    lab = ModelEngineeringLab(repository)
    lab.create(_spec())
    lab.start("model-exp-001")
    measurement = _measurement("champion", "case-1", quality=0.7, latency_ms=100.0)

    lab.record_measurement("model-exp-001", measurement)
    lab.record_measurement("model-exp-001", measurement)

    assert len(repository.get("model-exp-001").observations) == 2
    with pytest.raises(ValueError, match="evidence is immutable"):
        lab.record_measurement(
            "model-exp-001",
            _measurement("champion", "case-1", quality=0.71, latency_ms=100.0),
        )


def test_partial_metric_write_resumes_without_duplicate_evidence() -> None:
    repository = InMemoryExperimentRepository()
    lab = ModelEngineeringLab(repository)
    lab.create(_spec())
    lab.start("model-exp-001")
    ExperimentEngine(repository).record(
        "model-exp-001",
        MetricObservation(
            candidate_id="champion",
            replay_id="case-1",
            metric="quality",
            value=0.7,
        ),
    )

    snapshot = lab.record_measurement(
        "model-exp-001",
        _measurement("champion", "case-1", quality=0.7, latency_ms=100.0),
    )

    assert len(snapshot.observations) == 2


def test_challenger_is_recommended_only_when_quality_and_guardrail_pass() -> None:
    repository = InMemoryExperimentRepository()
    lab = ModelEngineeringLab(repository)
    lab.create(_spec())
    lab.start("model-exp-001")
    _record_complete_matrix(lab, challenger_latency_ms=115.0)

    recommendation = lab.complete("model-exp-001")

    assert recommendation.candidate_id == "challenger"
    assert recommendation.previous_champion_id == "champion"
    assert recommendation.requires_activation_approval is True
    assert recommendation.production_mutation_performed is False
    assert len(recommendation.evidence_sha256) == 64


def test_guardrail_regression_keeps_existing_champion() -> None:
    repository = InMemoryExperimentRepository()
    lab = ModelEngineeringLab(repository)
    lab.create(_spec())
    lab.start("model-exp-001")
    _record_complete_matrix(lab, challenger_latency_ms=150.0)

    recommendation = lab.complete("model-exp-001")

    assert recommendation.candidate_id == "champion"
    assert recommendation.production_mutation_performed is False


def test_sqlite_restart_preserves_exact_recommendation_evidence(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "model-engineering.sqlite3")
    store.initialize()
    first = ModelEngineeringLab(SQLiteExperimentRepository(store))
    first.create(_spec())
    first.start("model-exp-001")
    _record_complete_matrix(first, challenger_latency_ms=115.0)
    before_restart = first.complete("model-exp-001")

    after_restart = ModelEngineeringLab(SQLiteExperimentRepository(store)).recommendation(
        "model-exp-001"
    )

    assert after_restart == before_restart


def test_private_eval_rejects_candidate_without_private_data_capability() -> None:
    cloud = replace(
        _candidate("challenger", model_version="2.0.0"),
        provider_kind=ProviderKind.CLOUD,
        supports_private_data=False,
    )

    with pytest.raises(PermissionError, match="privacy exceeds"):
        ModelExperimentSpec(
            experiment_id="privacy-mismatch",
            champion=_candidate("champion", model_version="1.0.0"),
            challengers=(cloud,),
            cases=(_case("case-1", dataset_version="1"),),
            primary_metric=MetricDefinition("quality"),
        )


def test_create_start_and_complete_are_idempotent_for_uncertain_replay() -> None:
    repository = InMemoryExperimentRepository()
    lab = ModelEngineeringLab(repository)
    first = lab.create(_spec())
    assert lab.create(_spec()) == first
    running = lab.start("model-exp-001")
    assert lab.start("model-exp-001") == running
    _record_complete_matrix(lab, challenger_latency_ms=115.0)

    first_recommendation = lab.complete("model-exp-001")
    replayed_recommendation = lab.complete("model-exp-001")

    assert replayed_recommendation == first_recommendation
