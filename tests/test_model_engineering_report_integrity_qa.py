from __future__ import annotations

from dataclasses import replace

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
from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ProviderKind,
)


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
    cases = (
        CaseBenchmarkResult(
            candidate_id="candidate",
            case_id="pass",
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
        ),
        CaseBenchmarkResult(
            candidate_id="candidate",
            case_id="failure",
            score=0.0,
            passed=False,
            completion_succeeded=False,
            latency_ms=20.0,
            response_sha256=None,
            error_code=ModelErrorCode.UNAVAILABLE,
            input_tokens=None,
            output_tokens=None,
            total_tokens=None,
            resource_before=None,
            resource_after=None,
            accelerator_before=None,
            accelerator_after=None,
        ),
    )
    return CandidateBenchmarkReport(
        candidate=candidate,
        run=BenchmarkRunEvidence(
            run_id="qa-run",
            configuration_sha256=configuration_sha256,
        ),
        evaluation_set_id="held-out",
        evaluation_set_version="1",
        evaluation_set_sha256=evaluation_sha256,
        execution_config_sha256=config.evidence_sha256,
        evaluation_purpose=EvaluationPurpose.HELD_OUT,
        case_results=cases,
        weighted_quality_score=0.5,
        task_pass_rate=0.5,
        completion_rate=0.5,
        mean_latency_ms=10.0,
        p95_latency_ms=10.0,
        peak_cpu_percent=None,
        peak_memory_percent=None,
        min_available_memory_bytes=None,
        peak_accelerator_percent=None,
        peak_accelerator_memory_bytes=None,
    )


@pytest.mark.parametrize(
    "serializer",
    [benchmark_report_json, benchmark_accessible_report_json],
)
def test_public_report_evidence_rejects_aggregate_substitution(serializer) -> None:
    report = _report()

    with pytest.raises(ValueError):
        forged = replace(
            report,
            task_pass_rate=1.0,
            completion_rate=1.0,
            mean_latency_ms=0.0,
            p95_latency_ms=0.0,
        )
        serializer(forged)
