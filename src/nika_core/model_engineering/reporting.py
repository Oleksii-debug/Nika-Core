from __future__ import annotations

import hashlib
import json
from typing import Any

from nika_core.model_engineering.contracts import (
    AcceleratorSnapshot,
    BenchmarkSuiteReport,
    CandidateBenchmarkReport,
    CaseBenchmarkResult,
    validate_candidate_benchmark_report,
)
from nika_core.model_engineering.resource_evidence import (
    benchmark_resource_evidence_payload,
)
from nika_core.resources.contracts import ResourceSnapshot


def benchmark_report_json(report: CandidateBenchmarkReport) -> str:
    return json.dumps(
        benchmark_report_payload(report),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def benchmark_report_sha256(report: CandidateBenchmarkReport) -> str:
    return hashlib.sha256(benchmark_report_json(report).encode("utf-8")).hexdigest()


def benchmark_suite_json(report: BenchmarkSuiteReport) -> str:
    if type(report) is not BenchmarkSuiteReport:
        raise TypeError("report must be an exact BenchmarkSuiteReport")
    BenchmarkSuiteReport.__post_init__(report)
    payload = {
        "schema": "nika-model-benchmark-suite-v1",
        "evaluation_set_id": report.evaluation_set_id,
        "evaluation_set_version": report.evaluation_set_version,
        "evaluation_set_sha256": report.evaluation_set_sha256,
        "execution_config_sha256": report.execution_config_sha256,
        "reports": [benchmark_report_payload(item) for item in report.reports],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def benchmark_accessible_report_json(report: CandidateBenchmarkReport) -> str:
    """Serialize the stable semantic screen-reader report view."""

    return json.dumps(
        benchmark_accessible_report_payload(report),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def benchmark_accessible_report_payload(
    report: CandidateBenchmarkReport,
) -> dict[str, Any]:
    """Build the semantic report view without raw prompt/response/provider text."""

    if type(report) is not CandidateBenchmarkReport:
        raise TypeError("report must be an exact CandidateBenchmarkReport")
    validate_candidate_benchmark_report(report)
    resources = benchmark_resource_evidence_payload(report)
    failures = [
        {
            "case_id": result.case_id,
            "error_code": result.error_code.value,
        }
        for result in report.case_results
        if not result.completion_succeeded and result.error_code is not None
    ]
    return {
        "schema": "nika-model-benchmark-accessible-report-v1",
        "model": {
            "candidate_id": report.candidate.candidate_id,
            "model": report.candidate.expected_response_model,
            "candidate_evidence_sha256": report.candidate.evidence_sha256,
        },
        "provider": {
            "provider_id": report.candidate.provider_id,
            "provider_kind": report.candidate.provider_kind.value,
        },
        "dataset": {
            "evaluation_set_id": report.evaluation_set_id,
            "version": report.evaluation_set_version,
            "purpose": report.evaluation_purpose.value,
            "sha256": report.evaluation_set_sha256,
            "run_id": report.run.run_id,
            "configuration_sha256": report.run.configuration_sha256,
        },
        "quality": {
            "weighted_quality_score": report.weighted_quality_score,
            "task_pass_rate": report.task_pass_rate,
            "completion_rate": report.completion_rate,
        },
        "latency": {
            "mean_ms": report.mean_latency_ms,
            "p95_ms": report.p95_latency_ms,
        },
        "resources": resources,
        "failures": failures,
        "recommendation": {
            "status": "not_evaluated",
            "reason": "benchmark_evidence_only",
        },
        "evidence_limitations": _evidence_limitations(report, resources),
    }


def render_text_report(report: CandidateBenchmarkReport) -> str:
    """Render the semantic benchmark view as linear screen-reader-friendly text."""

    view = benchmark_accessible_report_payload(report)
    lines = [
        "Nika Core Model Engineering Lab benchmark",
        "Model",
        f"Candidate: {view['model']['candidate_id']}",
        f"Model: {view['model']['model']}",
        "Provider",
        (
            f"Provider: {view['provider']['provider_id']} "
            f"({view['provider']['provider_kind']})"
        ),
        "Dataset",
        (
            "Evaluation set: "
            f"{view['dataset']['evaluation_set_id']} "
            f"version {view['dataset']['version']} "
            f"({view['dataset']['purpose']})"
        ),
        f"Evaluation SHA-256: {view['dataset']['sha256']}",
        f"Candidate evidence SHA-256: {view['model']['candidate_evidence_sha256']}",
        f"Execution config SHA-256: {report.execution_config_sha256}",
        f"Run ID: {report.run.run_id}",
        f"Configuration SHA-256: {report.run.configuration_sha256}",
        "Quality",
        f"Weighted quality score: {report.weighted_quality_score:.6f}",
        f"Task pass rate: {report.task_pass_rate:.6f}",
        f"Completion rate: {report.completion_rate:.6f}",
        "Latency",
        f"Mean latency ms: {_optional_number(report.mean_latency_ms)}",
        f"P95 latency ms: {_optional_number(report.p95_latency_ms)}",
        "Resources",
        f"Peak CPU percent: {_optional_number(report.peak_cpu_percent)}",
        f"Peak memory percent: {_optional_number(report.peak_memory_percent)}",
        (
            "Minimum available memory bytes: "
            f"{_optional_integer(report.min_available_memory_bytes)}"
        ),
        (
            "Peak accelerator percent: "
            f"{_optional_number(report.peak_accelerator_percent)}"
        ),
        (
            "Peak accelerator memory bytes: "
            f"{_optional_integer(report.peak_accelerator_memory_bytes)}"
        ),
        "Failures",
    ]
    failures = view["failures"]
    if failures:
        for failure in failures:
            lines.append(f"- {failure['case_id']}: {failure['error_code']}")
    else:
        lines.append("- none")
    lines.extend(
        [
            "Recommendation",
            f"Status: {view['recommendation']['status']}",
            f"Reason: {view['recommendation']['reason']}",
            "Evidence limitations",
        ]
    )
    limitations = view["evidence_limitations"]
    if limitations:
        lines.extend(f"- {item}" for item in limitations)
    else:
        lines.append("- none")
    lines.append("Cases:")
    for result in report.case_results:
        status = "PASS" if result.passed else "FAIL"
        completion = "completed" if result.completion_succeeded else "provider_error"
        error = result.error_code.value if result.error_code is not None else "none"
        lines.append(
            f"- {result.case_id}: {status}; score={result.score:.6f}; "
            f"{completion}; latency_ms={result.latency_ms:.3f}; error={error}"
        )
    lines.append(f"Evidence SHA-256: {benchmark_report_sha256(report)}")
    return "\n".join(lines)


def _evidence_limitations(
    report: CandidateBenchmarkReport,
    resources: dict[str, Any],
) -> list[str]:
    limitations = [
        "human_nvda_verification_not_attested",
        "resource_sampling_is_point_in_time_not_continuous",
        "source_build_revision_not_attested",
    ]
    if report.mean_latency_ms is None:
        limitations.append("validated_success_latency_unavailable")
    for metric_name, metric in resources["summary"].items():
        if metric["status"] == "unknown":
            limitations.append(f"{metric_name}:{metric['unknown_reason']}")
    return limitations

def benchmark_report_payload(report: CandidateBenchmarkReport) -> dict[str, Any]:
    validate_candidate_benchmark_report(report)
    return {
        "schema": "nika-model-benchmark-report-v1",
        "candidate": {
            "candidate_id": report.candidate.candidate_id,
            "provider_id": report.candidate.provider_id,
            "provider_kind": report.candidate.provider_kind.value,
            "request_model": report.candidate.request_model,
            "expected_response_model": report.candidate.expected_response_model,
            "engine_provenance_ref": report.candidate.engine_provenance_ref,
            "engine_license_ref": report.candidate.engine_license_ref,
            "model_provenance_ref": report.candidate.model_provenance_ref,
            "model_license_ref": report.candidate.model_license_ref,
            "model_sha256": report.candidate.model_sha256,
            "evidence_sha256": report.candidate.evidence_sha256,
        },
        "execution_config_sha256": report.execution_config_sha256,
        "run": {
            "run_id": report.run.run_id,
            "configuration_sha256": report.run.configuration_sha256,
        },
        "evaluation_set": {
            "evaluation_set_id": report.evaluation_set_id,
            "version": report.evaluation_set_version,
            "sha256": report.evaluation_set_sha256,
            "purpose": report.evaluation_purpose.value,
        },
        "metrics": {
            "weighted_quality_score": report.weighted_quality_score,
            "task_pass_rate": report.task_pass_rate,
            "completion_rate": report.completion_rate,
            "mean_latency_ms": report.mean_latency_ms,
            "p95_latency_ms": report.p95_latency_ms,
            "peak_cpu_percent": report.peak_cpu_percent,
            "peak_memory_percent": report.peak_memory_percent,
            "min_available_memory_bytes": report.min_available_memory_bytes,
            "peak_accelerator_percent": report.peak_accelerator_percent,
            "peak_accelerator_memory_bytes": report.peak_accelerator_memory_bytes,
        },
        "resource_evidence": benchmark_resource_evidence_payload(report),
        "cases": [_case_payload(item) for item in report.case_results],
    }


def _case_payload(result: CaseBenchmarkResult) -> dict[str, Any]:
    return {
        "candidate_id": result.candidate_id,
        "case_id": result.case_id,
        "score": result.score,
        "evaluation_weight": result.evaluation_weight,
        "pass_score": result.pass_score,
        "passed": result.passed,
        "completion_succeeded": result.completion_succeeded,
        "latency_ms": result.latency_ms,
        "response_sha256": result.response_sha256,
        "error_code": result.error_code.value if result.error_code is not None else None,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "total_tokens": result.total_tokens,
        "resource_before": _resource_payload(result.resource_before),
        "resource_after": _resource_payload(result.resource_after),
        "accelerator_before": _accelerator_payload(result.accelerator_before),
        "accelerator_after": _accelerator_payload(result.accelerator_after),
    }


def _resource_payload(snapshot: ResourceSnapshot | None) -> dict[str, float | int] | None:
    if snapshot is None:
        return None
    return {
        "cpu_percent": float(snapshot.cpu_percent),
        "memory_percent": float(snapshot.memory_percent),
        "available_memory_bytes": snapshot.available_memory_bytes,
    }


def _accelerator_payload(
    snapshot: AcceleratorSnapshot | None,
) -> dict[str, float | int | None] | None:
    if snapshot is None:
        return None
    return {
        "utilization_percent": snapshot.utilization_percent,
        "memory_used_bytes": snapshot.memory_used_bytes,
    }


def _optional_number(value: float | None) -> str:
    return "not measured" if value is None else f"{value:.3f}"


def _optional_integer(value: int | None) -> str:
    return "not measured" if value is None else str(value)
