from __future__ import annotations

from dataclasses import replace

import pytest

from nika_core.model_engineering import (
    BenchmarkExecutionConfig,
    BenchmarkRunEvidence,
    BenchmarkSuiteReport,
    CandidateBenchmarkReport,
    CaseBenchmarkResult,
    EvaluationPurpose,
    ModelCandidate,
    benchmark_configuration_sha256,
)
from nika_core.model_gateway.contracts import ProviderKind
from nika_core.training_evaluation_attestation import (
    TrainingEvaluationAttestationError,
    attest_training_benchmark_suite,
)
from nika_core.training_evaluation_binding import TrainingEvaluationBinding

_BASE = "a" * 64
_CHALLENGER = "b" * 64
_EVAL = "c" * 64
_PACKAGE = "d" * 64
_DESCRIPTOR = "e" * 64
_REGISTRY = "f" * 64


def _binding() -> TrainingEvaluationBinding:
    return TrainingEvaluationBinding(
        job_id="job",
        base_candidate_id="base",
        challenger_candidate_id="challenger",
        base_sha256=_BASE,
        challenger_sha256=_CHALLENGER,
        candidate_artifact_ref="models/challenger",
        frozen_package_sha256=_PACKAGE,
        evaluation_set_sha256=_EVAL,
        descriptor_digest=_DESCRIPTOR,
        descriptor_registry_key=_REGISTRY,
        challenger_size_bytes=123,
    )


def _candidate(candidate_id: str, digest: str) -> ModelCandidate:
    return ModelCandidate(
        candidate_id=candidate_id,
        provider_id="local",
        provider_kind=ProviderKind.LOCAL,
        request_model=candidate_id,
        expected_response_model=candidate_id,
        engine_provenance_ref="engine",
        engine_license_ref="engine-license",
        model_provenance_ref=f"model:{candidate_id}",
        model_license_ref="model-license",
        model_sha256=digest,
    )


def _report(candidate: ModelCandidate, *, run_id: str) -> CandidateBenchmarkReport:
    config = BenchmarkExecutionConfig()
    configuration_sha256 = benchmark_configuration_sha256(
        candidate_evidence_sha256=candidate.evidence_sha256,
        evaluation_set_id="held-out",
        evaluation_set_version="1",
        evaluation_set_sha256=_EVAL,
        execution_config_sha256=config.evidence_sha256,
    )
    case = CaseBenchmarkResult(
        candidate_id=candidate.candidate_id,
        case_id="case",
        score=1.0,
        passed=True,
        completion_succeeded=True,
        latency_ms=1.0,
        response_sha256="1" * 64,
        error_code=None,
        input_tokens=1,
        output_tokens=1,
        total_tokens=2,
        resource_before=None,
        resource_after=None,
        accelerator_before=None,
        accelerator_after=None,
        loaded_artifact_sha256=candidate.model_sha256,
    )
    return CandidateBenchmarkReport(
        candidate=candidate,
        run=BenchmarkRunEvidence(
            run_id=run_id,
            configuration_sha256=configuration_sha256,
        ),
        evaluation_set_id="held-out",
        evaluation_set_version="1",
        evaluation_set_sha256=_EVAL,
        execution_config_sha256=config.evidence_sha256,
        evaluation_purpose=EvaluationPurpose.HELD_OUT,
        case_results=(case,),
        weighted_quality_score=1.0,
        task_pass_rate=1.0,
        completion_rate=1.0,
        mean_latency_ms=1.0,
        p95_latency_ms=1.0,
        peak_cpu_percent=None,
        peak_memory_percent=None,
        min_available_memory_bytes=None,
        peak_accelerator_percent=None,
        peak_accelerator_memory_bytes=None,
    )


def _suite() -> BenchmarkSuiteReport:
    base = _report(_candidate("base", _BASE), run_id="base-run")
    challenger = _report(
        _candidate("challenger", _CHALLENGER),
        run_id="challenger-run",
    )
    return BenchmarkSuiteReport(
        evaluation_set_id="held-out",
        evaluation_set_version="1",
        evaluation_set_sha256=_EVAL,
        execution_config_sha256=base.execution_config_sha256,
        reports=(base, challenger),
    )


def test_exact_old_new_loaded_bytes_bind_to_training_identity() -> None:
    receipt = attest_training_benchmark_suite(binding=_binding(), suite=_suite())

    assert receipt.base_model_sha256 == _BASE
    assert receipt.challenger_model_sha256 == _CHALLENGER
    assert len(receipt.base_report_sha256) == 64
    assert len(receipt.challenger_report_sha256) == 64
    assert len(receipt.evidence_sha256) == 64


def test_training_evaluation_attestation_rejects_challenger_digest_substitution() -> None:
    suite = _suite()
    challenger = suite.reports[1]
    forged_candidate = replace(challenger.candidate, model_sha256="9" * 64)
    object.__setattr__(challenger, "candidate", forged_candidate)

    with pytest.raises(
        TrainingEvaluationAttestationError,
        match="benchmark report evidence is not canonical|challenger benchmark candidate",
    ):
        attest_training_benchmark_suite(binding=_binding(), suite=suite)


def test_training_evaluation_attestation_rejects_missing_effect_attestation() -> None:
    suite = _suite()
    challenger = suite.reports[1]
    case = challenger.case_results[0]
    object.__setattr__(case, "loaded_artifact_sha256", None)

    with pytest.raises(
        TrainingEvaluationAttestationError,
        match="benchmark report evidence is not canonical|does not attest",
    ):
        attest_training_benchmark_suite(binding=_binding(), suite=suite)


def test_training_evaluation_attestation_rejects_wrong_held_out_identity() -> None:
    suite = _suite()
    object.__setattr__(suite, "evaluation_set_sha256", "8" * 64)

    with pytest.raises(
        TrainingEvaluationAttestationError,
        match="evaluation set|canonical",
    ):
        attest_training_benchmark_suite(binding=_binding(), suite=suite)


def test_training_evaluation_attestation_rejects_extra_candidate_report() -> None:
    suite = _suite()
    extra = _report(_candidate("extra", "7" * 64), run_id="extra-run")
    object.__setattr__(suite, "reports", (*suite.reports, extra))

    with pytest.raises(
        TrainingEvaluationAttestationError,
        match="exactly two|canonical",
    ):
        attest_training_benchmark_suite(binding=_binding(), suite=suite)


def test_attestation_digest_revalidates_constructor_bypass_mutation() -> None:
    receipt = attest_training_benchmark_suite(binding=_binding(), suite=_suite())
    object.__setattr__(receipt, "challenger_model_sha256", "not-a-digest")

    with pytest.raises(ValueError, match="challenger_model_sha256"):
        _ = receipt.evidence_sha256
