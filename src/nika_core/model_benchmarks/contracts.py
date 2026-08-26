from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Protocol

from nika_core.model_gateway.contracts import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    PrivacyClass,
    ProviderKind,
)
from nika_core.resources.contracts import ResourceSnapshot


def _require_token(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must not be empty")
    if value != value.strip():
        raise ValueError(f"{name} must not contain surrounding whitespace")


def _require_sha256(value: str, name: str) -> None:
    _require_token(value, name)
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")


def _require_finite_percent(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    number = float(value)
    if not isfinite(number) or not 0 <= number <= 100:
        raise ValueError(f"{name} must be finite and in the range [0, 100]")
    return number


def validate_resource_snapshot(snapshot: ResourceSnapshot) -> ResourceSnapshot:
    if not isinstance(snapshot, ResourceSnapshot):
        raise ValueError("resource snapshot must be a ResourceSnapshot")
    _require_finite_percent(snapshot.cpu_percent, "cpu_percent")
    _require_finite_percent(snapshot.memory_percent, "memory_percent")
    available = snapshot.available_memory_bytes
    if isinstance(available, bool) or not isinstance(available, int) or available < 0:
        raise ValueError("available_memory_bytes must be a non-negative integer")
    return snapshot


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    case_id: str
    messages: tuple[ModelMessage, ...]

    def __post_init__(self) -> None:
        _require_token(self.case_id, "case_id")
        if not isinstance(self.messages, tuple):
            raise ValueError("benchmark case messages must be a tuple")
        if not self.messages:
            raise ValueError("benchmark case requires at least one message")
        if any(not isinstance(message, ModelMessage) for message in self.messages):
            raise ValueError("benchmark case messages must contain only ModelMessage values")


@dataclass(frozen=True, slots=True)
class BenchmarkDataset:
    dataset_id: str
    version: str
    cases: tuple[BenchmarkCase, ...]

    def __post_init__(self) -> None:
        _require_token(self.dataset_id, "dataset_id")
        _require_token(self.version, "version")
        if not isinstance(self.cases, tuple):
            raise ValueError("benchmark dataset cases must be a tuple")
        if not self.cases:
            raise ValueError("benchmark dataset requires at least one case")
        if any(not isinstance(case, BenchmarkCase) for case in self.cases):
            raise ValueError("benchmark dataset cases must contain only BenchmarkCase values")
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("benchmark case IDs must be unique")


@dataclass(frozen=True, slots=True)
class ModelBenchmarkCandidate:
    candidate_id: str
    provider_id: str
    provider_kind: ProviderKind
    model: str
    privacy: PrivacyClass = PrivacyClass.PRIVATE
    timeout_seconds: float = 60.0
    expected_response_model: str | None = None
    license_reference: str | None = None
    artifact_digest: str | None = None

    def __post_init__(self) -> None:
        _require_token(self.candidate_id, "candidate_id")
        _require_token(self.provider_id, "provider_id")
        _require_token(self.model, "model")
        if not isinstance(self.provider_kind, ProviderKind):
            raise ValueError("provider_kind must be a ProviderKind")
        if not isinstance(self.privacy, PrivacyClass):
            raise ValueError("privacy must be a PrivacyClass")
        if isinstance(self.timeout_seconds, bool) or not isinstance(
            self.timeout_seconds, (int, float)
        ):
            raise ValueError("timeout_seconds must be numeric")
        if not isfinite(float(self.timeout_seconds)) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and greater than zero")
        for value, name in (
            (self.expected_response_model, "expected_response_model"),
            (self.license_reference, "license_reference"),
            (self.artifact_digest, "artifact_digest"),
        ):
            if value is not None:
                _require_token(value, name)


@dataclass(frozen=True, slots=True)
class ResourceEvidence:
    before: ResourceSnapshot
    after: ResourceSnapshot

    def __post_init__(self) -> None:
        validate_resource_snapshot(self.before)
        validate_resource_snapshot(self.after)


@dataclass(frozen=True, slots=True)
class BenchmarkCaseEvidence:
    run_id: str
    dataset_id: str
    dataset_version: str
    dataset_digest: str
    case_id: str
    case_digest: str
    candidate_id: str
    requested_provider_id: str
    provider_kind: ProviderKind
    requested_model: str
    response_model: str
    license_reference: str | None
    artifact_digest: str | None
    response_digest: str
    quality_score: float
    latency_ms: float
    resources: ResourceEvidence

    def __post_init__(self) -> None:
        if not isinstance(self.provider_kind, ProviderKind):
            raise ValueError("provider_kind must be a ProviderKind")
        for value, name in (
            (self.run_id, "run_id"),
            (self.dataset_id, "dataset_id"),
            (self.dataset_version, "dataset_version"),
            (self.case_id, "case_id"),
            (self.candidate_id, "candidate_id"),
            (self.requested_provider_id, "requested_provider_id"),
            (self.requested_model, "requested_model"),
            (self.response_model, "response_model"),
        ):
            _require_token(value, name)
        for value, name in (
            (self.dataset_digest, "dataset_digest"),
            (self.case_digest, "case_digest"),
            (self.response_digest, "response_digest"),
        ):
            _require_sha256(value, name)
        for value, name in (
            (self.license_reference, "license_reference"),
            (self.artifact_digest, "artifact_digest"),
        ):
            if value is not None:
                _require_token(value, name)
        if not isinstance(self.resources, ResourceEvidence):
            raise ValueError("resources must be ResourceEvidence")
        if isinstance(self.quality_score, bool) or not isinstance(
            self.quality_score, (int, float)
        ):
            raise ValueError("quality_score must be numeric")
        quality = float(self.quality_score)
        if not isfinite(quality) or not 0 <= quality <= 1:
            raise ValueError("quality_score must be finite and in the range [0, 1]")
        if isinstance(self.latency_ms, bool) or not isinstance(self.latency_ms, (int, float)):
            raise ValueError("latency_ms must be numeric")
        if not isfinite(float(self.latency_ms)) or self.latency_ms < 0:
            raise ValueError("latency_ms must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class BenchmarkRunEvidence:
    run_id: str
    dataset_id: str
    dataset_version: str
    dataset_digest: str
    candidate_id: str
    provider_id: str
    provider_kind: ProviderKind
    requested_model: str
    cases: tuple[BenchmarkCaseEvidence, ...]
    mean_quality_score: float
    mean_latency_ms: float

    def __post_init__(self) -> None:
        if not isinstance(self.provider_kind, ProviderKind):
            raise ValueError("provider_kind must be a ProviderKind")
        if not isinstance(self.cases, tuple):
            raise ValueError("benchmark run cases must be a tuple")
        if any(not isinstance(case, BenchmarkCaseEvidence) for case in self.cases):
            raise ValueError("benchmark run cases must contain only BenchmarkCaseEvidence values")
        for value, name in (
            (self.run_id, "run_id"),
            (self.dataset_id, "dataset_id"),
            (self.dataset_version, "dataset_version"),
            (self.candidate_id, "candidate_id"),
            (self.provider_id, "provider_id"),
            (self.requested_model, "requested_model"),
        ):
            _require_token(value, name)
        _require_sha256(self.dataset_digest, "dataset_digest")
        if not self.cases:
            raise ValueError("benchmark run evidence requires at least one case")
        if isinstance(self.mean_quality_score, bool) or not isinstance(
            self.mean_quality_score, (int, float)
        ):
            raise ValueError("mean_quality_score must be numeric")
        mean_quality = float(self.mean_quality_score)
        if not isfinite(mean_quality) or not 0 <= mean_quality <= 1:
            raise ValueError("mean_quality_score must be finite and in the range [0, 1]")
        if isinstance(self.mean_latency_ms, bool) or not isinstance(
            self.mean_latency_ms, (int, float)
        ):
            raise ValueError("mean_latency_ms must be numeric")
        mean_latency = float(self.mean_latency_ms)
        if not isfinite(mean_latency) or mean_latency < 0:
            raise ValueError("mean_latency_ms must be finite and non-negative")
        for case in self.cases:
            if case.run_id != self.run_id:
                raise ValueError("case evidence run_id mismatch")
            if case.dataset_id != self.dataset_id:
                raise ValueError("case evidence dataset_id mismatch")
            if case.dataset_version != self.dataset_version:
                raise ValueError("case evidence dataset_version mismatch")
            if case.dataset_digest != self.dataset_digest:
                raise ValueError("case evidence dataset_digest mismatch")
            if case.candidate_id != self.candidate_id:
                raise ValueError("case evidence candidate_id mismatch")
            if case.requested_provider_id != self.provider_id:
                raise ValueError("case evidence provider_id mismatch")
            if case.provider_kind is not self.provider_kind:
                raise ValueError("case evidence provider_kind mismatch")
            if case.requested_model != self.requested_model:
                raise ValueError("case evidence requested_model mismatch")


class ModelCompletionPort(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse: ...


class BenchmarkScorer(Protocol):
    def score(self, case: BenchmarkCase, response_text: str) -> float: ...


class MonotonicClockPort(Protocol):
    def now(self) -> float: ...
