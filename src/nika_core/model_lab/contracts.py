from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from math import isfinite
from typing import Mapping, Protocol

from nika_core.experiments.contracts import PromotionPolicy
from nika_core.model_gateway.contracts import ModelMessage, ModelResponse, PrivacyClass


def _required_text(value: str, name: str) -> str:
    if not value.strip():
        raise ValueError(f"{name} must not be empty")
    if value != value.strip():
        raise ValueError(f"{name} must not contain surrounding whitespace")
    return value


@dataclass(frozen=True, slots=True)
class BenchmarkCandidate:
    candidate_id: str
    provider_id: str
    model: str
    expected_model_id: str
    version: str
    artifact_ref: str
    permission_fingerprint: str

    def __post_init__(self) -> None:
        for value, name in (
            (self.candidate_id, "candidate_id"),
            (self.provider_id, "provider_id"),
            (self.model, "model"),
            (self.expected_model_id, "expected_model_id"),
            (self.version, "version"),
            (self.artifact_ref, "artifact_ref"),
            (self.permission_fingerprint, "permission_fingerprint"),
        ):
            _required_text(value, name)


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    case_id: str
    dataset_ref: str
    dataset_version: str
    messages: tuple[ModelMessage, ...]
    reference: tuple[tuple[str, str], ...] = ()
    privacy: PrivacyClass = PrivacyClass.PRIVATE
    timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        for value, name in (
            (self.case_id, "case_id"),
            (self.dataset_ref, "dataset_ref"),
            (self.dataset_version, "dataset_version"),
        ):
            _required_text(value, name)
        if not self.messages:
            raise ValueError("benchmark case must contain at least one message")
        if not isfinite(float(self.timeout_seconds)) or self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and greater than zero")
        keys = [key for key, _ in self.reference]
        if len(keys) != len(set(keys)):
            raise ValueError("benchmark reference keys must be unique")
        for key, value in self.reference:
            _required_text(key, "reference key")
            if not isinstance(value, str):
                raise TypeError("benchmark reference values must be strings")

    def reference_map(self) -> dict[str, str]:
        return dict(self.reference)


@dataclass(frozen=True, slots=True)
class BenchmarkPlan:
    benchmark_id: str
    champion: BenchmarkCandidate
    challengers: tuple[BenchmarkCandidate, ...]
    cases: tuple[BenchmarkCase, ...]
    policy: PromotionPolicy
    temperature: float | None = 0.0
    metadata: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        _required_text(self.benchmark_id, "benchmark_id")
        if not self.challengers:
            raise ValueError("at least one benchmark challenger is required")
        if not self.cases:
            raise ValueError("at least one benchmark case is required")
        if self.temperature is not None:
            if not isfinite(float(self.temperature)) or not 0 <= self.temperature <= 2:
                raise ValueError("temperature must be finite and between 0 and 2")
        candidate_ids = [
            self.champion.candidate_id,
            *(item.candidate_id for item in self.challengers),
        ]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("benchmark candidate IDs must be unique")
        case_ids = [item.case_id for item in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("benchmark case IDs must be unique")
        metadata_keys = [key for key, _ in self.metadata]
        if len(metadata_keys) != len(set(metadata_keys)):
            raise ValueError("benchmark metadata keys must be unique")
        for key, value in self.metadata:
            _required_text(key, "metadata key")
            if not isinstance(value, str):
                raise TypeError("benchmark metadata values must be strings")


class BenchmarkEvaluator(Protocol):
    @property
    def evaluator_id(self) -> str: ...

    @property
    def version(self) -> str: ...

    def evaluate(self, case: BenchmarkCase, response: ModelResponse) -> Mapping[str, float]: ...


class ExactTextMatchEvaluator:
    """Deterministic baseline evaluator for versioned text fixtures."""

    evaluator_id = "text.exact_match"
    version = "1"

    def __init__(
        self,
        *,
        reference_key: str = "expected_text",
        metric: str = "exact_match",
    ) -> None:
        self._reference_key = _required_text(reference_key, "reference_key")
        self._metric = _required_text(metric, "metric")

    def evaluate(self, case: BenchmarkCase, response: ModelResponse) -> Mapping[str, float]:
        reference = case.reference_map()
        if self._reference_key not in reference:
            raise ValueError(
                f"benchmark case is missing reference key: {self._reference_key}"
            )
        return {
            self._metric: 1.0
            if response.text == reference[self._reference_key]
            else 0.0
        }


def canonical_digest(payload: object) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
