from __future__ import annotations

import pytest

from nika_core.model_engineering import (
    AcceleratorSnapshot,
    BenchmarkExecutionConfig,
    BenchmarkRunEvidence,
    BenchmarkSuiteReport,
    CandidateBenchmarkReport,
    CaseBenchmarkResult,
    EvaluationPurpose,
    ModelCandidate,
    benchmark_accessible_report_json,
    benchmark_configuration_sha256,
    benchmark_report_json,
    benchmark_suite_json,
    render_text_report,
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
    accelerator = AcceleratorSnapshot(
        utilization_percent=10.0,
        memory_used_bytes=100,
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
        accelerator_before=accelerator,
        accelerator_after=None,
    )
    return CandidateBenchmarkReport(
        candidate=candidate,
        run=BenchmarkRunEvidence(
            run_id="accelerator-revalidation",
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
        peak_accelerator_percent=10.0,
        peak_accelerator_memory_bytes=100,
    )


@pytest.mark.parametrize(
    "serializer",
    (
        benchmark_report_json,
        benchmark_accessible_report_json,
        render_text_report,
    ),
)
@pytest.mark.parametrize(
    ("snapshot_field", "report_field", "invalid_value"),
    (
        ("utilization_percent", "peak_accelerator_percent", -1.0),
        ("memory_used_bytes", "peak_accelerator_memory_bytes", -1),
    ),
)
def test_public_reports_reject_mutated_nested_accelerator_evidence(
    serializer,
    snapshot_field,
    report_field,
    invalid_value,
) -> None:
    report = _report()
    snapshot = report.case_results[0].accelerator_before
    assert snapshot is not None

    object.__setattr__(snapshot, snapshot_field, invalid_value)
    object.__setattr__(report, report_field, invalid_value)

    with pytest.raises(ValueError):
        serializer(report)


@pytest.mark.parametrize(
    "serializer",
    (
        benchmark_report_json,
        benchmark_accessible_report_json,
        render_text_report,
    ),
)
def test_public_reports_reject_mutated_candidate_identity(serializer) -> None:
    report = _report()
    candidate = report.candidate

    object.__setattr__(candidate, "provider_id", " padded-provider ")
    object.__setattr__(
        report.run,
        "configuration_sha256",
        benchmark_configuration_sha256(
            candidate_evidence_sha256=candidate.evidence_sha256,
            evaluation_set_id=report.evaluation_set_id,
            evaluation_set_version=report.evaluation_set_version,
            evaluation_set_sha256=report.evaluation_set_sha256,
            execution_config_sha256=report.execution_config_sha256,
        ),
    )

    with pytest.raises(ValueError, match="surrounding whitespace"):
        serializer(report)


@pytest.mark.parametrize(
    "serializer",
    (
        benchmark_report_json,
        benchmark_accessible_report_json,
        render_text_report,
    ),
)
def test_public_reports_reject_mutated_run_identity(serializer) -> None:
    report = _report()
    object.__setattr__(report.run, "run_id", "bad run id")

    with pytest.raises(ValueError, match="safe ASCII identity"):
        serializer(report)


def test_suite_serializer_revalidates_outer_identity() -> None:
    report = _report()
    suite = BenchmarkSuiteReport(
        evaluation_set_id=report.evaluation_set_id,
        evaluation_set_version=report.evaluation_set_version,
        evaluation_set_sha256=report.evaluation_set_sha256,
        execution_config_sha256=report.execution_config_sha256,
        reports=(report,),
    )
    object.__setattr__(suite, "evaluation_set_id", " padded-suite ")

    with pytest.raises(ValueError, match="surrounding whitespace"):
        benchmark_suite_json(suite)
