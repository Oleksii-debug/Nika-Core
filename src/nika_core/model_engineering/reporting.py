from __future__ import annotations

import hashlib
import json
from typing import Any

from nika_core.model_engineering.contracts import (
    AcceleratorSnapshot,
    BenchmarkSuiteReport,
    CandidateBenchmarkReport,
    CaseBenchmarkResult,
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
    payload = {
        "schema": "nika-model-benchmark-suite-v1",
        "evaluation_set_id": report.evaluation_set_id,
        "evaluation_set_version": report.evaluation_set_version,
        "evaluation_set_sha256": report.evaluation_set_sha256,
        "reports": [benchmark_report_payload(item) for item in report.reports],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def render_text_report(report: CandidateBenchmarkReport) -> str:
    """Render a linear screen-reader-friendly report without prompt/response text."""

    lines = [
        "Nika Core Model Engineering Lab benchmark",
        f"Candidate: {report.candidate.candidate_id}",
        f"Provider: {report.candidate.provider_id} ({report.candidate.provider_kind.value})",
        f"Model: {report.candidate.expected_response_model}",
        (
            "Evaluation set: "
            f"{report.evaluation_set_id} version {report.evaluation_set_version} "
            f"({report.evaluation_purpose.value})"
        ),
        f"Evaluation SHA-256: {report.evaluation_set_sha256}",
        f"Candidate evidence SHA-256: {report.candidate.evidence_sha256}",
        f"Weighted quality score: {report.weighted_quality_score:.6f}",
        f"Task pass rate: {report.task_pass_rate:.6f}",
        f"Completion rate: {report.completion_rate:.6f}",
        f"Mean latency ms: {report.mean_latency_ms:.3f}",
        f"P95 latency ms: {report.p95_latency_ms:.3f}",
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
        "Cases:",
    ]
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


def benchmark_report_payload(report: CandidateBenchmarkReport) -> dict[str, Any]:
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
        "cases": [_case_payload(item) for item in report.case_results],
    }


def _case_payload(result: CaseBenchmarkResult) -> dict[str, Any]:
    return {
        "candidate_id": result.candidate_id,
        "case_id": result.case_id,
        "score": result.score,
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
