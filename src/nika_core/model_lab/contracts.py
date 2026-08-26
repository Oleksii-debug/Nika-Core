from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from typing import Protocol

from nika_core.experiments.contracts import ExperimentStatus
from nika_core.model_gateway.contracts import ModelMessage, ModelResponse, PrivacyClass


class EvaluationSplit(StrEnum):
    HELD_OUT = "held_out"
    REPLAY = "replay"


@dataclass(frozen=True, slots=True)
class ModelCandidate:
    candidate_id: str
    version: str
    provider_id: str
    model: str
    permission_fingerprint: str

    def __post_init__(self) -> None:
        for value, name in (
            (self.candidate_id, "candidate_id"),
            (self.version, "version"),
            (self.provider_id, "provider_id"),
            (self.model, "model"),
            (self.permission_fingerprint, "permission_fingerprint"),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be empty")
            if value != value.strip():
                raise ValueError(f"{name} must not contain surrounding whitespace")

    @property
    def artifact_ref(self) -> str:
        return f"model://{self.provider_id}/{self.model}"


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    case_id: str
    messages: tuple[ModelMessage, ...]
    expected_text: str

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise ValueError("case_id must not be empty")
        if self.case_id != self.case_id.strip():
            raise ValueError("case_id must not contain surrounding whitespace")
        if not self.messages:
            raise ValueError("evaluation case must contain at least one message")


@dataclass(frozen=True, slots=True)
class EvaluationSuite:
    dataset_ref: str
    dataset_version: str
    split: EvaluationSplit
    cases: tuple[EvaluationCase, ...]
    privacy: PrivacyClass = PrivacyClass.PRIVATE

    def __post_init__(self) -> None:
        for value, name in (
            (self.dataset_ref, "dataset_ref"),
            (self.dataset_version, "dataset_version"),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be empty")
            if value != value.strip():
                raise ValueError(f"{name} must not contain surrounding whitespace")
        if not self.cases:
            raise ValueError("evaluation suite must contain at least one case")
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("evaluation case IDs must be unique")

    @property
    def content_sha256(self) -> str:
        payload = [
            {
                "case_id": case.case_id,
                "messages": [
                    {"role": message.role, "content": message.content}
                    for message in case.messages
                ],
                "expected_text": case.expected_text,
            }
            for case in self.cases
        ]
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @property
    def evidence_version(self) -> str:
        return (
            f"{self.dataset_version};split={self.split.value};"
            f"sha256={self.content_sha256}"
        )


@dataclass(frozen=True, slots=True)
class ModelBenchmarkPolicy:
    minimum_quality_improvement: float = 0.0
    max_latency_regression_ms: float = 0.0
    minimum_cases: int = 1
    request_timeout_seconds: float = 60.0
    max_host_cpu_regression_percent: float | None = None
    max_host_memory_regression_percent: float | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.minimum_quality_improvement, "minimum_quality_improvement"),
            (self.max_latency_regression_ms, "max_latency_regression_ms"),
            (self.request_timeout_seconds, "request_timeout_seconds"),
        ):
            if not isfinite(float(value)):
                raise ValueError(f"{name} must be finite")
        if self.minimum_quality_improvement < 0:
            raise ValueError("minimum_quality_improvement must be non-negative")
        if self.max_latency_regression_ms < 0:
            raise ValueError("max_latency_regression_ms must be non-negative")
        if self.minimum_cases < 1:
            raise ValueError("minimum_cases must be at least 1")
        if self.request_timeout_seconds <= 0:
            raise ValueError("request_timeout_seconds must be greater than zero")
        for value, name in (
            (self.max_host_cpu_regression_percent, "max_host_cpu_regression_percent"),
            (self.max_host_memory_regression_percent, "max_host_memory_regression_percent"),
        ):
            if value is not None and (not isfinite(float(value)) or value < 0):
                raise ValueError(f"{name} must be finite and non-negative")


class QualityScorer(Protocol):
    def score(self, case: EvaluationCase, response: ModelResponse) -> float: ...


class ExactMatchScorer:
    def score(self, case: EvaluationCase, response: ModelResponse) -> float:
        return 1.0 if response.text == case.expected_text else 0.0


@dataclass(frozen=True, slots=True)
class CandidateBenchmarkSummary:
    candidate_id: str
    quality_mean: float
    latency_mean_ms: float
    host_cpu_mean_percent: float | None = None
    host_memory_mean_percent: float | None = None


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    experiment_id: str
    status: ExperimentStatus
    selected_candidate_id: str | None
    dataset_sha256: str
    summaries: tuple[CandidateBenchmarkSummary, ...]

    @property
    def promoted(self) -> bool:
        return self.status is ExperimentStatus.PROMOTED
