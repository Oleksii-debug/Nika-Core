from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
import re
from typing import Protocol

from nika_core.model_gateway.contracts import (
    ModelMessage,
    PrivacyClass,
    ProviderKind,
)
from nika_core.resources.contracts import ResourceSnapshot

_STABLE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


def _require_text(value: str, name: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be empty")
    if value != value.strip():
        raise ValueError(f"{name} must not contain surrounding whitespace")


def _require_stable_id(value: str, name: str) -> None:
    if not _STABLE_ID.fullmatch(value):
        raise ValueError(
            f"{name} must match {_STABLE_ID.pattern} and contain no path separators"
        )


@dataclass(frozen=True, slots=True)
class ModelCandidate:
    candidate_id: str
    provider_id: str
    provider_kind: ProviderKind
    model: str
    model_version: str
    license_reference: str
    provenance_reference: str
    permission_fingerprint: str
    artifact_sha256: str | None = None

    def __post_init__(self) -> None:
        _require_stable_id(self.candidate_id, "candidate_id")
        for value, name in (
            (self.provider_id, "provider_id"),
            (self.model, "model"),
            (self.model_version, "model_version"),
            (self.license_reference, "license_reference"),
            (self.provenance_reference, "provenance_reference"),
            (self.permission_fingerprint, "permission_fingerprint"),
        ):
            _require_text(value, name)
        if self.artifact_sha256 is not None and not _SHA256.fullmatch(self.artifact_sha256):
            raise ValueError("artifact_sha256 must be exactly 64 hexadecimal characters")
        if self.provider_kind is ProviderKind.LOCAL and self.artifact_sha256 is None:
            raise ValueError("local model candidates require artifact_sha256 evidence")


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    case_id: str
    messages: tuple[ModelMessage, ...]
    dataset_ref: str
    dataset_version: str
    scorer_id: str
    privacy: PrivacyClass = PrivacyClass.PRIVATE
    reference_text: str | None = None
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_stable_id(self.case_id, "case_id")
        if not self.messages:
            raise ValueError("benchmark case must contain at least one model message")
        for value, name in (
            (self.dataset_ref, "dataset_ref"),
            (self.dataset_version, "dataset_version"),
        ):
            _require_text(value, name)
        _require_stable_id(self.scorer_id, "scorer_id")
        if self.reference_text is not None and not self.reference_text:
            raise ValueError("reference_text must be non-empty when provided")
        if len(set(self.tags)) != len(self.tags):
            raise ValueError("benchmark case tags must be unique")
        for tag in self.tags:
            _require_stable_id(tag, "tag")


@dataclass(frozen=True, slots=True)
class BenchmarkSuite:
    suite_id: str
    version: str
    cases: tuple[BenchmarkCase, ...]
    repetitions: int = 1
    timeout_seconds: float = 60.0
    temperature: float | None = None

    def __post_init__(self) -> None:
        _require_stable_id(self.suite_id, "suite_id")
        _require_text(self.version, "version")
        if not self.cases:
            raise ValueError("benchmark suite must contain at least one case")
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("benchmark case IDs must be unique")
        if not 1 <= self.repetitions <= 20:
            raise ValueError("repetitions must be between 1 and 20")
        if len(self.cases) * self.repetitions > 1000:
            raise ValueError("benchmark suite is limited to 1000 model attempts")
        if not 0 < self.timeout_seconds <= 600:
            raise ValueError("timeout_seconds must be in the range (0, 600]")
        if self.temperature is not None and not 0 <= self.temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")


@dataclass(frozen=True, slots=True)
class MetricValue:
    metric: str
    value: float

    def __post_init__(self) -> None:
        _require_text(self.metric, "metric")
        if not isfinite(float(self.value)):
            raise ValueError("metric value must be finite")


class BenchmarkScorer(Protocol):
    @property
    def scorer_id(self) -> str: ...

    def score(self, case: BenchmarkCase, response_text: str) -> tuple[MetricValue, ...]: ...


class AttemptStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class BenchmarkAttempt:
    attempt_id: str
    case_id: str
    repetition: int
    status: AttemptStatus
    request_id: str
    prompt_sha256: str
    reference_sha256: str | None
    provider_id: str
    provider_kind: ProviderKind
    model: str
    wall_latency_ms: float
    metrics: tuple[MetricValue, ...] = ()
    response_sha256: str | None = None
    response_characters: int | None = None
    provider_latency_ms: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    resource_before: ResourceSnapshot | None = None
    resource_after: ResourceSnapshot | None = None
    error_code: str | None = None

    def __post_init__(self) -> None:
        _require_stable_id(self.attempt_id, "attempt_id")
        _require_stable_id(self.case_id, "case_id")
        _require_text(self.request_id, "request_id")
        if self.repetition < 1:
            raise ValueError("repetition must be at least 1")
        if not _SHA256.fullmatch(self.prompt_sha256):
            raise ValueError("prompt_sha256 must be a SHA-256 digest")
        if self.reference_sha256 is not None and not _SHA256.fullmatch(self.reference_sha256):
            raise ValueError("reference_sha256 must be a SHA-256 digest")
        if self.wall_latency_ms < 0 or not isfinite(float(self.wall_latency_ms)):
            raise ValueError("wall_latency_ms must be finite and non-negative")
        metric_names = [metric.metric for metric in self.metrics]
        if len(metric_names) != len(set(metric_names)):
            raise ValueError("metric names must be unique within one attempt")
        if self.provider_latency_ms is not None:
            if self.provider_latency_ms < 0 or not isfinite(float(self.provider_latency_ms)):
                raise ValueError("provider_latency_ms must be finite and non-negative")
        for name, value in (
            ("input_tokens", self.input_tokens),
            ("output_tokens", self.output_tokens),
            ("total_tokens", self.total_tokens),
        ):
            if value is not None and value < 0:
                raise ValueError(f"{name} must be non-negative")
        if self.status is AttemptStatus.SUCCESS:
            if self.response_sha256 is None or self.response_characters is None:
                raise ValueError("successful attempt requires response fingerprint and size")
            if not _SHA256.fullmatch(self.response_sha256):
                raise ValueError("response_sha256 must be a SHA-256 digest")
            if self.response_characters < 0:
                raise ValueError("response_characters must be non-negative")
            if self.error_code is not None:
                raise ValueError("successful attempt cannot contain error_code")
        else:
            if self.error_code is None:
                raise ValueError("failed attempt requires error_code")
            _require_text(self.error_code, "error_code")
            if self.metrics or self.response_sha256 is not None:
                raise ValueError("failed attempt cannot contain quality metrics or response digest")


@dataclass(frozen=True, slots=True)
class BenchmarkRunEvidence:
    run_id: str
    candidate: ModelCandidate
    suite_id: str
    suite_version: str
    suite_sha256: str
    expected_attempts: int
    attempts: tuple[BenchmarkAttempt, ...]

    def __post_init__(self) -> None:
        _require_stable_id(self.run_id, "run_id")
        _require_stable_id(self.suite_id, "suite_id")
        _require_text(self.suite_version, "suite_version")
        if not _SHA256.fullmatch(self.suite_sha256):
            raise ValueError("suite_sha256 must be a SHA-256 digest")
        if self.expected_attempts < 1:
            raise ValueError("expected_attempts must be at least 1")
        attempt_ids = [attempt.attempt_id for attempt in self.attempts]
        if len(attempt_ids) != len(set(attempt_ids)):
            raise ValueError("benchmark attempt IDs must be unique")

    @property
    def complete(self) -> bool:
        return (
            len(self.attempts) == self.expected_attempts
            and all(attempt.status is AttemptStatus.SUCCESS for attempt in self.attempts)
        )
