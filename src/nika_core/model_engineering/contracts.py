from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from math import ceil, isfinite
from statistics import fmean
from typing import Protocol

from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelMessage,
    PrivacyClass,
    ProviderKind,
)
from nika_core.resources.contracts import ResourceSnapshot

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _identity(value: str, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be canonical text")
    if not value or value != value.strip():
        raise ValueError(f"{name} must be non-empty without surrounding whitespace")
    if any(not char.isprintable() for char in value):
        raise ValueError(f"{name} must not contain control characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must be valid UTF-8 text") from exc
    return value


def _sha256(value: str, name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be canonical text")
    if not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _optional_sha256(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    return _sha256(value, name)


def _run_id(value: str) -> str:
    if type(value) is not str:
        raise TypeError("run_id must be canonical text")
    if not _RUN_ID_RE.fullmatch(value):
        raise ValueError("run_id must use 1..128 safe ASCII identity characters")
    return value


def benchmark_configuration_sha256(
    *,
    candidate_evidence_sha256: str,
    evaluation_set_id: str,
    evaluation_set_version: str,
    evaluation_set_sha256: str,
    execution_config_sha256: str,
) -> str:
    """Hash the comparable benchmark configuration, excluding per-attempt run identity."""

    payload = {
        "schema": "nika-model-benchmark-configuration-v1",
        "candidate_evidence_sha256": _sha256(
            candidate_evidence_sha256,
            "candidate_evidence_sha256",
        ),
        "evaluation_set_id": _identity(evaluation_set_id, "evaluation_set_id"),
        "evaluation_set_version": _identity(
            evaluation_set_version,
            "evaluation_set_version",
        ),
        "evaluation_set_sha256": _sha256(
            evaluation_set_sha256,
            "evaluation_set_sha256",
        ),
        "execution_config_sha256": _sha256(
            execution_config_sha256,
            "execution_config_sha256",
        ),
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _bounded_percent(value: float | None, name: str) -> float | None:
    if value is None:
        return None
    if type(value) not in (int, float):
        raise TypeError(f"{name} must be numeric")
    number = float(value)
    if not isfinite(number) or not 0 <= number <= 100:
        raise ValueError(f"{name} must be finite and in [0, 100]")
    return number


class EvaluationPurpose(StrEnum):
    DEVELOPMENT = "development"
    HELD_OUT = "held_out"


@dataclass(frozen=True, slots=True)
class BenchmarkExecutionConfig:
    timeout_seconds: float = 60.0
    temperature: float | None = 0.0
    scorer_id: str = "exact-match-nfc-v1"

    def __post_init__(self) -> None:
        if type(self.timeout_seconds) not in (int, float):
            raise TypeError("timeout_seconds must be numeric")
        timeout = float(self.timeout_seconds)
        if not isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout_seconds must be finite and greater than zero")
        object.__setattr__(self, "timeout_seconds", timeout)
        _identity(self.scorer_id, "scorer_id")
        if self.temperature is None:
            return
        if type(self.temperature) not in (int, float):
            raise TypeError("temperature must be numeric")
        temperature = float(self.temperature)
        if not isfinite(temperature) or not 0 <= temperature <= 2:
            raise ValueError("temperature must be finite and in [0, 2]")
        object.__setattr__(self, "temperature", temperature)

    @property
    def evidence_sha256(self) -> str:
        payload = {
            "schema": "nika-model-benchmark-execution-config-v1",
            "scorer_id": self.scorer_id,
            "temperature": self.temperature,
            "timeout_seconds": self.timeout_seconds,
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class BenchmarkRunEvidence:
    run_id: str
    configuration_sha256: str

    def __post_init__(self) -> None:
        _run_id(self.run_id)
        _sha256(self.configuration_sha256, "configuration_sha256")


@dataclass(frozen=True, slots=True)
class ModelCandidate:
    candidate_id: str
    provider_id: str
    provider_kind: ProviderKind
    request_model: str
    expected_response_model: str
    engine_provenance_ref: str
    engine_license_ref: str
    model_provenance_ref: str
    model_license_ref: str
    model_sha256: str | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.candidate_id, "candidate_id"),
            (self.provider_id, "provider_id"),
            (self.request_model, "request_model"),
            (self.expected_response_model, "expected_response_model"),
            (self.engine_provenance_ref, "engine_provenance_ref"),
            (self.engine_license_ref, "engine_license_ref"),
            (self.model_provenance_ref, "model_provenance_ref"),
            (self.model_license_ref, "model_license_ref"),
        ):
            _identity(value, name)
        if not any(self.provider_kind is member for member in ProviderKind):
            raise TypeError("provider_kind must be a ProviderKind")
        _optional_sha256(self.model_sha256, "model_sha256")

    @property
    def evidence_sha256(self) -> str:
        payload = {
            "schema": "nika-model-candidate-v1",
            "candidate_id": self.candidate_id,
            "provider_id": self.provider_id,
            "provider_kind": self.provider_kind.value,
            "request_model": self.request_model,
            "expected_response_model": self.expected_response_model,
            "engine_provenance_ref": self.engine_provenance_ref,
            "engine_license_ref": self.engine_license_ref,
            "model_provenance_ref": self.model_provenance_ref,
            "model_license_ref": self.model_license_ref,
            "model_sha256": self.model_sha256,
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    case_id: str
    messages: tuple[ModelMessage, ...]
    expected_text: str
    pass_score: float = 1.0
    weight: float = 1.0

    def __post_init__(self) -> None:
        _identity(self.case_id, "case_id")
        if type(self.messages) is not tuple:
            raise TypeError("evaluation messages must be a canonical tuple")
        if not self.messages:
            raise ValueError("evaluation case requires at least one message")
        if any(type(message) is not ModelMessage for message in self.messages):
            raise TypeError("evaluation messages must use exact ModelMessage values")
        for message in self.messages:
            try:
                message.content.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise ValueError(
                    "evaluation message content must be valid UTF-8 text"
                ) from exc
        if type(self.expected_text) is not str:
            raise TypeError("expected_text must be canonical text")
        if not self.expected_text:
            raise ValueError("expected_text must not be empty")
        try:
            self.expected_text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("expected_text must be valid UTF-8 text") from exc
        if type(self.pass_score) not in (int, float):
            raise TypeError("pass_score must be numeric")
        score = float(self.pass_score)
        if not isfinite(score) or not 0 <= score <= 1:
            raise ValueError("pass_score must be finite and in [0, 1]")
        if type(self.weight) not in (int, float):
            raise TypeError("weight must be numeric")
        weight = float(self.weight)
        if not isfinite(weight) or weight <= 0:
            raise ValueError("weight must be finite and greater than zero")


@dataclass(frozen=True, slots=True)
class EvaluationSet:
    evaluation_set_id: str
    version: str
    provenance_ref: str
    license_ref: str
    purpose: EvaluationPurpose
    privacy: PrivacyClass
    cases: tuple[EvaluationCase, ...]

    def __post_init__(self) -> None:
        for value, name in (
            (self.evaluation_set_id, "evaluation_set_id"),
            (self.version, "version"),
            (self.provenance_ref, "provenance_ref"),
            (self.license_ref, "license_ref"),
        ):
            _identity(value, name)
        if not any(self.purpose is member for member in EvaluationPurpose):
            raise TypeError("purpose must be an EvaluationPurpose")
        if not any(self.privacy is member for member in PrivacyClass):
            raise TypeError("privacy must be a PrivacyClass")
        if type(self.cases) is not tuple:
            raise TypeError("evaluation cases must be a canonical tuple")
        if not self.cases:
            raise ValueError("evaluation set requires at least one case")
        if any(type(case) is not EvaluationCase for case in self.cases):
            raise TypeError("evaluation cases must use exact EvaluationCase values")
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("evaluation case IDs must be unique")

    @property
    def content_sha256(self) -> str:
        payload = {
            "schema": "nika-model-evaluation-set-v1",
            "evaluation_set_id": self.evaluation_set_id,
            "version": self.version,
            "provenance_ref": self.provenance_ref,
            "license_ref": self.license_ref,
            "purpose": self.purpose.value,
            "privacy": self.privacy.value,
            "cases": [
                {
                    "case_id": case.case_id,
                    "messages": [
                        {"role": message.role, "content": message.content}
                        for message in case.messages
                    ],
                    "expected_text": case.expected_text,
                    "pass_score": float(case.pass_score),
                    "weight": float(case.weight),
                }
                for case in self.cases
            ],
        }
        encoded = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def validate_model_candidate(candidate: ModelCandidate) -> None:
    """Revalidate an exact candidate carrier before trusted use."""

    if type(candidate) is not ModelCandidate:
        raise TypeError("candidate must be an exact ModelCandidate")
    ModelCandidate.__post_init__(candidate)


def validate_evaluation_set(evaluation_set: EvaluationSet) -> None:
    """Revalidate the complete evaluation graph before execution or promotion."""

    if type(evaluation_set) is not EvaluationSet:
        raise TypeError("evaluation_set must be an exact EvaluationSet")
    EvaluationSet.__post_init__(evaluation_set)
    for case in evaluation_set.cases:
        EvaluationCase.__post_init__(case)
        for message in case.messages:
            ModelMessage.__post_init__(message)


@dataclass(frozen=True, slots=True)
class AcceleratorSnapshot:
    utilization_percent: float | None = None
    memory_used_bytes: int | None = None

    def __post_init__(self) -> None:
        _bounded_percent(self.utilization_percent, "utilization_percent")
        if self.memory_used_bytes is not None and (
            type(self.memory_used_bytes) is not int or self.memory_used_bytes < 0
        ):
            raise ValueError("memory_used_bytes must be a non-negative integer")


class AcceleratorObserverPort(Protocol):
    def snapshot(self) -> AcceleratorSnapshot: ...


@dataclass(frozen=True, slots=True)
class CaseBenchmarkResult:
    candidate_id: str
    case_id: str
    score: float
    passed: bool
    completion_succeeded: bool
    latency_ms: float
    response_sha256: str | None
    error_code: ModelErrorCode | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    resource_before: ResourceSnapshot | None
    resource_after: ResourceSnapshot | None
    accelerator_before: AcceleratorSnapshot | None
    accelerator_after: AcceleratorSnapshot | None
    loaded_artifact_sha256: str | None = None
    evaluation_weight: float = 1.0
    pass_score: float = 1.0

    def __post_init__(self) -> None:
        _identity(self.candidate_id, "candidate_id")
        _identity(self.case_id, "case_id")
        if type(self.score) not in (int, float):
            raise TypeError("score must be numeric")
        if type(self.passed) is not bool:
            raise TypeError("passed must be boolean")
        if type(self.completion_succeeded) is not bool:
            raise TypeError("completion_succeeded must be boolean")
        score = float(self.score)
        if not isfinite(score) or not 0 <= score <= 1:
            raise ValueError("score must be finite and in [0, 1]")
        if type(self.evaluation_weight) not in (int, float):
            raise TypeError("evaluation_weight must be numeric")
        evaluation_weight = float(self.evaluation_weight)
        if not isfinite(evaluation_weight) or evaluation_weight <= 0:
            raise ValueError("evaluation_weight must be finite and greater than zero")
        if type(self.pass_score) not in (int, float):
            raise TypeError("pass_score must be numeric")
        pass_score = float(self.pass_score)
        if not isfinite(pass_score) or not 0 <= pass_score <= 1:
            raise ValueError("pass_score must be finite and in [0, 1]")
        if type(self.latency_ms) not in (int, float):
            raise TypeError("latency_ms must be numeric")
        latency = float(self.latency_ms)
        if not isfinite(latency) or latency < 0:
            raise ValueError("latency_ms must be finite and non-negative")
        _optional_sha256(self.response_sha256, "response_sha256")
        _optional_sha256(
            self.loaded_artifact_sha256,
            "loaded_artifact_sha256",
        )
        for snapshot, name in (
            (self.resource_before, "resource_before"),
            (self.resource_after, "resource_after"),
        ):
            if snapshot is not None and type(snapshot) is not ResourceSnapshot:
                raise TypeError(f"{name} must be an exact ResourceSnapshot")
        for snapshot, name in (
            (self.accelerator_before, "accelerator_before"),
            (self.accelerator_after, "accelerator_after"),
        ):
            if snapshot is None:
                continue
            if type(snapshot) is not AcceleratorSnapshot:
                raise TypeError(f"{name} must be an exact AcceleratorSnapshot")
            AcceleratorSnapshot.__post_init__(snapshot)
        for value, name in (
            (self.input_tokens, "input_tokens"),
            (self.output_tokens, "output_tokens"),
            (self.total_tokens, "total_tokens"),
        ):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{name} must be a non-negative integer")
        if (
            self.total_tokens is not None
            and self.input_tokens is not None
            and self.output_tokens is not None
            and self.total_tokens < self.input_tokens + self.output_tokens
        ):
            raise ValueError("total_tokens is smaller than known token components")
        if self.error_code is not None and not any(
            self.error_code is member for member in ModelErrorCode
        ):
            raise TypeError("error_code must be a ModelErrorCode")
        if self.completion_succeeded:
            if self.passed is not (score >= pass_score):
                raise ValueError(
                    "passed must match successful score and pass_score evidence"
                )
            if self.error_code is not None:
                raise ValueError("successful completion cannot carry error_code")
            if self.response_sha256 is None:
                raise ValueError("successful completion requires response_sha256")
        else:
            if self.error_code is None:
                raise ValueError("failed completion requires error_code")
            if self.passed or float(self.score) != 0.0:
                raise ValueError("failed completion cannot carry passing quality evidence")
            if self.response_sha256 is not None:
                raise ValueError("failed completion cannot carry response_sha256")
            if self.loaded_artifact_sha256 is not None:
                raise ValueError(
                    "failed completion cannot carry loaded artifact attestation"
                )
            if any(
                value is not None
                for value in (self.input_tokens, self.output_tokens, self.total_tokens)
            ):
                raise ValueError("failed completion cannot carry token evidence")


@dataclass(frozen=True, slots=True)
class CandidateBenchmarkReport:
    candidate: ModelCandidate
    run: BenchmarkRunEvidence
    evaluation_set_id: str
    evaluation_set_version: str
    evaluation_set_sha256: str
    execution_config_sha256: str
    evaluation_purpose: EvaluationPurpose
    case_results: tuple[CaseBenchmarkResult, ...]
    weighted_quality_score: float
    task_pass_rate: float
    completion_rate: float
    mean_latency_ms: float | None
    p95_latency_ms: float | None
    peak_cpu_percent: float | None
    peak_memory_percent: float | None
    min_available_memory_bytes: int | None
    peak_accelerator_percent: float | None
    peak_accelerator_memory_bytes: int | None

    def __post_init__(self) -> None:
        if type(self.candidate) is not ModelCandidate:
            raise TypeError("candidate must be an exact ModelCandidate")
        if type(self.run) is not BenchmarkRunEvidence:
            raise TypeError("run must be an exact BenchmarkRunEvidence")
        _identity(self.evaluation_set_id, "evaluation_set_id")
        _identity(self.evaluation_set_version, "evaluation_set_version")
        _sha256(self.evaluation_set_sha256, "evaluation_set_sha256")
        _sha256(self.execution_config_sha256, "execution_config_sha256")
        expected_configuration = benchmark_configuration_sha256(
            candidate_evidence_sha256=self.candidate.evidence_sha256,
            evaluation_set_id=self.evaluation_set_id,
            evaluation_set_version=self.evaluation_set_version,
            evaluation_set_sha256=self.evaluation_set_sha256,
            execution_config_sha256=self.execution_config_sha256,
        )
        if self.run.configuration_sha256 != expected_configuration:
            raise ValueError("benchmark run configuration identity mismatch")
        if not any(self.evaluation_purpose is member for member in EvaluationPurpose):
            raise TypeError("evaluation_purpose must be an EvaluationPurpose")
        if type(self.case_results) is not tuple:
            raise TypeError("case_results must be a canonical tuple")
        if not self.case_results:
            raise ValueError("benchmark report requires case results")
        if any(type(item) is not CaseBenchmarkResult for item in self.case_results):
            raise TypeError("case_results must use exact CaseBenchmarkResult values")
        if any(item.candidate_id != self.candidate.candidate_id for item in self.case_results):
            raise ValueError("case result candidate identity mismatch")
        successful = tuple(
            item for item in self.case_results if item.completion_succeeded
        )
        if self.candidate.model_sha256 is None:
            if any(item.loaded_artifact_sha256 is not None for item in successful):
                raise ValueError(
                    "unpinned candidate cannot carry loaded artifact attestation"
                )
        elif any(
            item.loaded_artifact_sha256 != self.candidate.model_sha256
            for item in successful
        ):
            raise ValueError(
                "successful benchmark case loaded artifact identity mismatch"
            )
        case_ids = [item.case_id for item in self.case_results]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("case result IDs must be unique")
        for value, name in (
            (self.weighted_quality_score, "weighted_quality_score"),
            (self.task_pass_rate, "task_pass_rate"),
            (self.completion_rate, "completion_rate"),
        ):
            if type(value) not in (int, float):
                raise TypeError(f"{name} must be numeric")
            number = float(value)
            if not isfinite(number) or not 0 <= number <= 1:
                raise ValueError(f"{name} must be finite and in [0, 1]")
        for value, name in (
            (self.mean_latency_ms, "mean_latency_ms"),
            (self.p95_latency_ms, "p95_latency_ms"),
        ):
            if value is None:
                continue
            if type(value) not in (int, float):
                raise TypeError(f"{name} must be numeric")
            number = float(value)
            if not isfinite(number) or number < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        _bounded_percent(self.peak_cpu_percent, "peak_cpu_percent")
        _bounded_percent(self.peak_memory_percent, "peak_memory_percent")
        _bounded_percent(self.peak_accelerator_percent, "peak_accelerator_percent")
        for value, name in (
            (self.min_available_memory_bytes, "min_available_memory_bytes"),
            (self.peak_accelerator_memory_bytes, "peak_accelerator_memory_bytes"),
        ):
            if value is not None and (type(value) is not int or value < 0):
                raise ValueError(f"{name} must be a non-negative integer")


def validate_candidate_benchmark_report(report: CandidateBenchmarkReport) -> None:
    """Reject report aggregates that disagree with the exact case evidence."""

    if type(report) is not CandidateBenchmarkReport:
        raise TypeError("report must be an exact CandidateBenchmarkReport")
    if type(report.candidate) is not ModelCandidate:
        raise TypeError("candidate must be an exact ModelCandidate")
    if type(report.run) is not BenchmarkRunEvidence:
        raise TypeError("run must be an exact BenchmarkRunEvidence")
    ModelCandidate.__post_init__(report.candidate)
    BenchmarkRunEvidence.__post_init__(report.run)
    CandidateBenchmarkReport.__post_init__(report)
    results = report.case_results
    if type(results) is not tuple:
        raise TypeError("case_results must be a canonical tuple")
    for item in results:
        if type(item) is not CaseBenchmarkResult:
            raise TypeError("case_results must use exact CaseBenchmarkResult values")
        CaseBenchmarkResult.__post_init__(item)
    total_weight = sum(float(item.evaluation_weight) for item in results)
    expected_quality = sum(
        float(item.score) * float(item.evaluation_weight)
        for item in results
    ) / total_weight
    expected_pass_rate = sum(item.passed for item in results) / len(results)
    expected_completion_rate = (
        sum(item.completion_succeeded for item in results) / len(results)
    )
    successful_latencies = [
        float(item.latency_ms)
        for item in results
        if item.completion_succeeded
    ]
    expected_mean_latency = (
        fmean(successful_latencies) if successful_latencies else None
    )
    expected_p95_latency = None
    if successful_latencies:
        ordered = sorted(successful_latencies)
        index = max(0, ceil(0.95 * len(ordered)) - 1)
        expected_p95_latency = ordered[index]

    resource_snapshots = tuple(
        snapshot
        for item in results
        for snapshot in (item.resource_before, item.resource_after)
        if snapshot is not None
    )
    accelerator_snapshots = tuple(
        snapshot
        for item in results
        for snapshot in (item.accelerator_before, item.accelerator_after)
        if snapshot is not None
    )
    accelerator_utilization = tuple(
        float(snapshot.utilization_percent)
        for snapshot in accelerator_snapshots
        if snapshot.utilization_percent is not None
    )
    accelerator_memory = tuple(
        snapshot.memory_used_bytes
        for snapshot in accelerator_snapshots
        if snapshot.memory_used_bytes is not None
    )

    expected = (
        expected_quality,
        expected_pass_rate,
        expected_completion_rate,
        expected_mean_latency,
        expected_p95_latency,
        max(
            (float(snapshot.cpu_percent) for snapshot in resource_snapshots),
            default=None,
        ),
        max(
            (float(snapshot.memory_percent) for snapshot in resource_snapshots),
            default=None,
        ),
        min(
            (snapshot.available_memory_bytes for snapshot in resource_snapshots),
            default=None,
        ),
        max(accelerator_utilization, default=None),
        max(accelerator_memory, default=None),
    )
    actual = (
        report.weighted_quality_score,
        report.task_pass_rate,
        report.completion_rate,
        report.mean_latency_ms,
        report.p95_latency_ms,
        report.peak_cpu_percent,
        report.peak_memory_percent,
        report.min_available_memory_bytes,
        report.peak_accelerator_percent,
        report.peak_accelerator_memory_bytes,
    )
    if actual != expected:
        raise ValueError(
            "benchmark report aggregate metrics do not match case evidence"
        )


@dataclass(frozen=True, slots=True)
class BenchmarkSuiteReport:
    evaluation_set_id: str
    evaluation_set_version: str
    evaluation_set_sha256: str
    execution_config_sha256: str
    reports: tuple[CandidateBenchmarkReport, ...]

    def __post_init__(self) -> None:
        _identity(self.evaluation_set_id, "evaluation_set_id")
        _identity(self.evaluation_set_version, "evaluation_set_version")
        _sha256(self.evaluation_set_sha256, "evaluation_set_sha256")
        _sha256(self.execution_config_sha256, "execution_config_sha256")
        if type(self.reports) is not tuple:
            raise TypeError("benchmark suite reports must be a canonical tuple")
        if not self.reports:
            raise ValueError("benchmark suite requires at least one candidate report")
        if any(type(report) is not CandidateBenchmarkReport for report in self.reports):
            raise TypeError("benchmark suite reports must use exact candidate reports")
        ids = [report.candidate.candidate_id for report in self.reports]
        if len(ids) != len(set(ids)):
            raise ValueError("benchmark suite candidate IDs must be unique")
        run_ids = [report.run.run_id for report in self.reports]
        if len(run_ids) != len(set(run_ids)):
            raise ValueError("benchmark suite run IDs must be unique")
        for report in self.reports:
            if (
                report.evaluation_set_id != self.evaluation_set_id
                or report.evaluation_set_version != self.evaluation_set_version
                or report.evaluation_set_sha256 != self.evaluation_set_sha256
                or report.execution_config_sha256 != self.execution_config_sha256
            ):
                raise ValueError("benchmark suite mixes evaluation/config identities")
