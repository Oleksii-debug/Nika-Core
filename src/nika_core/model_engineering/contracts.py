from __future__ import annotations

from dataclasses import dataclass, field
from math import isfinite
from urllib.parse import parse_qsl, urlsplit

from nika_core.model_gateway.contracts import PrivacyClass, ProviderKind

_SECRET_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "code",
        "credential",
        "id_token",
        "key",
        "password",
        "secret",
        "sig",
        "signature",
        "token",
        "x-amz-signature",
        "x-goog-signature",
    }
)


def _require_text(value: str, name: str) -> None:
    if not value.strip():
        raise ValueError(f"{name} must not be empty")
    if value != value.strip():
        raise ValueError(f"{name} must not contain surrounding whitespace")


def _require_sha256(value: str, name: str) -> None:
    _require_text(value, name)
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")


def _require_non_secret_ref(value: str, name: str) -> None:
    """Reject common credential-bearing URL forms before durable evidence persistence."""

    _require_text(value, name)
    try:
        parsed = urlsplit(value)
        query = parse_qsl(parsed.query, keep_blank_values=True)
    except ValueError as exc:
        raise ValueError(f"{name} must be a valid non-secret reference") from exc
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{name} must not contain URL credentials")
    for key, _ in query:
        if key.casefold() in _SECRET_QUERY_KEYS:
            raise ValueError(f"{name} must not contain credential-like query parameters")


@dataclass(frozen=True, slots=True)
class ModelCandidate:
    """Immutable, non-secret identity for one model/provider candidate."""

    candidate_id: str
    provider_id: str
    provider_kind: ProviderKind
    model_id: str
    model_version: str
    source_ref: str
    license_ref: str
    privacy_capability_ref: str
    permission_fingerprint: str
    supports_private_data: bool
    artifact_sha256: str | None = None

    def __post_init__(self) -> None:
        for value, name in (
            (self.candidate_id, "candidate_id"),
            (self.provider_id, "provider_id"),
            (self.model_id, "model_id"),
            (self.model_version, "model_version"),
            (self.permission_fingerprint, "permission_fingerprint"),
        ):
            _require_text(value, name)
        for value, name in (
            (self.source_ref, "source_ref"),
            (self.license_ref, "license_ref"),
            (self.privacy_capability_ref, "privacy_capability_ref"),
        ):
            _require_non_secret_ref(value, name)
        if not isinstance(self.provider_kind, ProviderKind):
            raise TypeError("provider_kind must be ProviderKind")
        if not isinstance(self.supports_private_data, bool):
            raise TypeError("supports_private_data must be bool")
        if self.artifact_sha256 is not None:
            _require_sha256(self.artifact_sha256, "artifact_sha256")


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    """Versioned evaluation identity; raw prompts/answers are intentionally not persisted here."""

    case_id: str
    dataset_ref: str
    dataset_version: str
    dataset_sha256: str
    privacy: PrivacyClass = PrivacyClass.PRIVATE

    def __post_init__(self) -> None:
        _require_text(self.case_id, "case_id")
        _require_non_secret_ref(self.dataset_ref, "dataset_ref")
        _require_text(self.dataset_version, "dataset_version")
        _require_sha256(self.dataset_sha256, "dataset_sha256")
        if not isinstance(self.privacy, PrivacyClass):
            raise TypeError("privacy must be PrivacyClass")


@dataclass(frozen=True, slots=True)
class MetricDefinition:
    name: str
    higher_is_better: bool = True
    max_regression: float = 0.0

    def __post_init__(self) -> None:
        _require_text(self.name, "metric name")
        if not isinstance(self.higher_is_better, bool):
            raise TypeError("higher_is_better must be bool")
        value = float(self.max_regression)
        if not isfinite(value) or value < 0:
            raise ValueError("max_regression must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class MetricValue:
    name: str
    value: float

    def __post_init__(self) -> None:
        _require_text(self.name, "metric name")
        if not isfinite(float(self.value)):
            raise ValueError("metric value must be finite")


@dataclass(frozen=True, slots=True)
class CaseMeasurement:
    candidate_id: str
    case_id: str
    metrics: tuple[MetricValue, ...]

    def __post_init__(self) -> None:
        _require_text(self.candidate_id, "candidate_id")
        _require_text(self.case_id, "case_id")
        if not self.metrics:
            raise ValueError("measurement must contain metrics")
        names = [item.name for item in self.metrics]
        if len(names) != len(set(names)):
            raise ValueError("measurement metric names must be unique")


@dataclass(frozen=True, slots=True)
class ModelExperimentSpec:
    experiment_id: str
    champion: ModelCandidate
    challengers: tuple[ModelCandidate, ...]
    cases: tuple[EvaluationCase, ...]
    primary_metric: MetricDefinition
    guardrails: tuple[MetricDefinition, ...] = ()
    minimum_improvement: float = 0.0

    def __post_init__(self) -> None:
        _require_text(self.experiment_id, "experiment_id")
        if not self.challengers:
            raise ValueError("at least one challenger is required")
        if not self.cases:
            raise ValueError("at least one evaluation case is required")
        improvement = float(self.minimum_improvement)
        if not isfinite(improvement) or improvement < 0:
            raise ValueError("minimum_improvement must be finite and non-negative")
        candidates = (self.champion, *self.challengers)
        candidate_ids = [item.candidate_id for item in candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("candidate IDs must be unique")
        permissions = {item.permission_fingerprint for item in candidates}
        if len(permissions) != 1:
            raise PermissionError("model candidates may not widen or alter permissions")
        if any(
            case.privacy is not PrivacyClass.PUBLIC and not candidate.supports_private_data
            for case in self.cases
            for candidate in candidates
        ):
            raise PermissionError("evaluation privacy exceeds a candidate privacy capability")
        case_ids = [item.case_id for item in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("evaluation case IDs must be unique")
        metric_names = [self.primary_metric.name, *(item.name for item in self.guardrails)]
        if len(metric_names) != len(set(metric_names)):
            raise ValueError("experiment metric names must be unique")


@dataclass(frozen=True, slots=True)
class ModelRecommendation:
    experiment_id: str
    candidate_id: str
    candidate_manifest_sha256: str
    evidence_sha256: str
    previous_champion_id: str
    requires_activation_approval: bool = field(default=True, init=False)
    production_mutation_performed: bool = field(default=False, init=False)
