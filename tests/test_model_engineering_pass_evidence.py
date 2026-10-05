from __future__ import annotations

from dataclasses import replace

import json

import pytest

from nika_core.model_engineering import (
    BenchmarkExecutionConfig,
    BenchmarkRunEvidence,
    CandidateBenchmarkReport,
    CaseBenchmarkResult,
    EvaluationPurpose,
    ModelCandidate,
    benchmark_accessible_report_json,
    benchmark_configuration_sha256,
    benchmark_report_json,
)
from nika_core.model_gateway.contracts import ProviderKind


def _report() -> CandidateBenchmarkReport:
    candidate = ModelCandidate(
        candidate_id="candidate",
        provider_id="local",
        provider_kind=ProviderKind.LOCAL,
        request_model="model",
        expected_response_model="model",
        engine_provenance_ref="engine:1",
        engine_license_ref="license:engine",
        model_provenance_ref="model:1",
        model_license_ref="license:model",
    )
    config = BenchmarkExecutionConfig()
    evaluation_sha256 = "a" * 64
    configuration_sha256 = benchmark_configuration_sha256(
        candidate_evidence_sha256=candidate.evidence_sha256,
        evaluation_set_id="held-out",
        evaluation_set_version="1",
        evaluation_set_sha256=evaluation_sha256,
        execution_config_sha256=config.evidence_sha256,
    )
    case = CaseBenchmarkResult(
        candidate_id="candidate",
        case_id="case",
        evaluation_weight=1.0,
        pass_score=1.0,
        score=1.0,
        passed=True,
        completion_succeeded=True,
        latency_ms=10.0,
        response_sha256="b" * 64,
        error_code=None,
        input_tokens=1,
        output_tokens=1,
        total_tokens=2,
        resource_before=None,
        resource_after=None,
        accelerator_before=None,
        accelerator_after=None,
    )
    return CandidateBenchmarkReport(
        candidate=candidate,
        run=BenchmarkRunEvidence(
            run_id="threshold-run",
            configuration_sha256=configuration_sha256,
        ),
        evaluation_set_id="held-out",
        evaluation_set_version="1",
        evaluation_set_sha256=evaluation_sha256,
        execution_config_sha256=config.evidence_sha256,
        evaluation_purpose=EvaluationPurpose.HELD_OUT,
        case_results=(case,),
        weighted_quality_score=1.0,
        task_pass_rate=1.0,
        completion_rate=1.0,
        mean_latency_ms=10.0,
        p95_latency_ms=10.0,
        peak_cpu_percent=None,
        peak_memory_percent=None,
        min_available_memory_bytes=None,
        peak_accelerator_percent=None,
        peak_accelerator_memory_bytes=None,
    )


def test_successful_case_rejects_pass_below_bound_threshold() -> None:
    with pytest.raises(ValueError, match="pass_score evidence"):
        CaseBenchmarkResult(
            candidate_id="candidate",
            case_id="case",
            evaluation_weight=1.0,
            pass_score=0.75,
            score=0.5,
            passed=True,
            completion_succeeded=True,
            latency_ms=10.0,
            response_sha256="b" * 64,
            error_code=None,
            input_tokens=1,
            output_tokens=1,
            total_tokens=2,
            resource_before=None,
            resource_after=None,
            accelerator_before=None,
            accelerator_after=None,
        )


@pytest.mark.parametrize(
    "serializer",
    [benchmark_report_json, benchmark_accessible_report_json],
)
def test_public_serializers_revalidate_exact_case_pass_evidence(serializer) -> None:
    report = _report()
    case = report.case_results[0]

    object.__setattr__(case, "score", 0.5)
    object.__setattr__(report, "weighted_quality_score", 0.5)

    with pytest.raises(ValueError, match="pass_score evidence"):
        serializer(report)


def test_machine_evidence_carries_pass_threshold() -> None:
    payload = json.loads(benchmark_report_json(_report()))

    assert payload["cases"][0]["pass_score"] == 1.0


def test_case_evidence_rejects_total_smaller_than_known_components() -> None:
    case = _report().case_results[0]

    with pytest.raises(ValueError, match="total_tokens"):
        replace(case, total_tokens=1)


def test_public_serializer_revalidates_token_total_evidence() -> None:
    report = _report()
    case = report.case_results[0]
    object.__setattr__(case, "total_tokens", 1)

    with pytest.raises(ValueError, match="total_tokens"):
        benchmark_report_json(report)
