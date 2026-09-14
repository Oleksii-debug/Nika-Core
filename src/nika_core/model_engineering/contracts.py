from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from typing import Protocol

from nika_core.model_gateway.contracts import (
    ModelErrorCode,
    ModelMessage,
    PrivacyClass,
    ProviderKind,
)
from nika_core.resources.contracts import ResourceSnapshot

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _identity(value: str, name: str) -> str:
    if not value or value != value.strip():
        raise ValueError(f"{name} must be non-empty without surrounding whitespace")
    return value


def _optional_sha256(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    if not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _bounded_percent(value: float | None, name: str) -> float | None:
    if value is None:
        return None
    number = float(value)
    if not isfinite(number) or not 0 <= number <= 100:
        raise ValueError(f"{name} must be finite and in [0, 100]")
    return number


class EvaluationPurpose(StrEnum):
    DEVELOPMENT = "development"
    HELD_OUT = "held_out"


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
        if not isinstance(self.provider_kind, ProviderKind):
            raise ValueError("provider_kind must be a ProviderKind")
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
        if not self.messages:
            raise ValueError("evaluation case requires at least one message")
        if any(not isinstance(message, ModelMessage) for message in self.messages):
            raise ValueError("evaluation messages must use ModelMessage")
        if not self.expected_text:
            raise ValueError("expected_text must not be empty")
        if isinstance(self.pass_score, bool):
            raise ValueError("pass_score must not be boolean")
        score = float(self.pass_score)
        if not isfinite(score) or not 0 <= score <= 1:
            raise ValueError("pass_score must be finite and in [0, 1]")
        if isinstance(self.weight, bool):
            raise ValueError("weight must not be boolean")
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
        if not isinstance(self.purpose, EvaluationPurpose):
            raise ValueError("purpose must be an EvaluationPurpose")
        if not isinstance(self.privacy, PrivacyClass):
            raise ValueError("privacy must be a PrivacyClass")
        if not self.cases:
            raise ValueError("evaluation set requires at least one case")
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


@dataclass(frozen=True, slots=True)
class AcceleratorSnapshot:
    utilization_percent: float | None = None
    memory_used_bytes: int | None = None

    def __post_init__(self) -> None:
        _bounded_percent(self.utilization_percent, "utilization_percent")
        if self.memory_used_bytes is not None:
            if (
                isinstance(self.memory_used_bytes, bool)
                or not isinstance(self.memory_used_bytes, int)
                or self.memory_used_bytes < 0
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

    def __post_init__(self) -> None:
        _identity(self.candidate_id, "candidate_id")
        _identity(self.case_id, "case_id")
        score = float(self.score)
        if not isfinite(score) or not 0 <= score <= 1:
            raise ValueError("score must be finite and in [0, 1]")
        latency = float(self.latency_ms)
        if not isfinite(latency) or latency < 0:
            raise ValueError("latency_ms must be finite and non-negative")
        _optional_sha256(self.response_sha256, "response_sha256")
        for value, name in (
            (self.input_tokens, "input_tokens"),
            (self.output_tokens, "output_tokens"),
            (self.total_tokens, "total_tokens"),
        ):
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer")
        if self.completion_succeeded and self.error_code is not None:
            raise ValueError("successful completion cannot carry error_code")
        if not self.completion_succeeded and self.error_code is None:
            raise ValueError("failed completion requires error_code")


@dataclass(frozen=True, slots=True)
class CandidateBenchmarkReport:
    candidate: ModelCandidate
    evaluation_set_id: str
    evaluation_set_version: str
    evaluation_set_sha256: str
    evaluation_purpose: EvaluationPurpose
    case_results: tuple[CaseBenchmarkResult, ...]
    weighted_quality_score: float
    task_pass_rate: float
    completion_rate: float
    mean_latency_ms: float
    p95_latency_ms: float
    peak_cpu_percent: float | None
    peak_memory_percent: float | None
    min_available_memory_bytes: int | None
    peak_accelerator_percent: float | None
    peak_accelerator_memory_bytes: int | None

    def __post_init__(self) -> None:
        _identity(self.evaluation_set_id, "evaluation_set_id")
        _identity(self.evaluation_set_version, "evaluation_set_version")
        _optional_sha256(self.evaluation_set_sha256, "evaluation_set_sha256")
        if not self.case_results:
            raise ValueError("benchmark report requires case results")
        if any(item.candidate_id != self.candidate.candidate_id for item in self.case_results):
            raise ValueError("case result candidate identity mismatch")
        case_ids = [item.case_id for item in self.case_results]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("case result IDs must be unique")
        for value, name in (
            (self.weighted_quality_score, "weighted_quality_score"),
            (self.task_pass_rate, "task_pass_rate"),
            (self.completion_rate, "completion_rate"),
        ):
            number = float(value)
            if not isfinite(number) or not 0 <= number <= 1:
                raise ValueError(f"{name} must be finite and in [0, 1]")
        for value, name in (
            (self.mean_latency_ms, "mean_latency_ms"),
            (self.p95_latency_ms, "p95_latency_ms"),
        ):
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
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class BenchmarkSuiteReport:
    evaluation_set_id: str
    evaluation_set_version: str
    evaluation_set_sha256: str
    reports: tuple[CandidateBenchmarkReport, ...]

    def __post_init__(self) -> None:
        _identity(self.evaluation_set_id, "evaluation_set_id")
        _identity(self.evaluation_set_version, "evaluation_set_version")
        _optional_sha256(self.evaluation_set_sha256, "evaluation_set_sha256")
        if not self.reports:
            raise ValueError("benchmark suite requires at least one candidate report")
        ids = [report.candidate.candidate_id for report in self.reports]
        if len(ids) != len(set(ids)):
            raise ValueError("benchmark suite candidate IDs must be unique")
        for report in self.reports:
            if (
                report.evaluation_set_id != self.evaluation_set_id
                or report.evaluation_set_version != self.evaluation_set_version
                or report.evaluation_set_sha256 != self.evaluation_set_sha256
            ):
                raise ValueError("benchmark suite mixes evaluation set identities")
