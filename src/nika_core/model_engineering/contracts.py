from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SECRET_PREFIXES = ("sk-", "ghp_", "github_pat_", "bearer ")
_MAX_IDENTIFIER_LENGTH = 240


def _require_identifier(name: str, value: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value or value != value.strip():
        raise ValueError(f"{name} must be non-empty and have no surrounding whitespace")
    if len(value) > _MAX_IDENTIFIER_LENGTH:
        raise ValueError(f"{name} exceeds {_MAX_IDENTIFIER_LENGTH} characters")
    if any(ord(character) < 32 for character in value):
        raise ValueError(f"{name} must not contain control characters")
    lowered = value.casefold()
    if lowered.startswith(_SECRET_PREFIXES):
        raise ValueError(f"{name} looks like secret material and is not allowed")
    return value


def _require_sha256(name: str, value: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 hex digest")
    return value


def _require_finite(name: str, value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be numeric")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError(f"{name} must be finite")
    return normalized


def _require_aware_datetime(name: str, value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def payload_sha256(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


class MetricDirection(StrEnum):
    MAXIMIZE = "maximize"
    MINIMIZE = "minimize"


@dataclass(frozen=True, slots=True)
class ModelCandidate:
    provider_id: str
    model_id: str
    revision: str | None = None

    def __post_init__(self) -> None:
        _require_identifier("provider_id", self.provider_id)
        _require_identifier("model_id", self.model_id)
        if self.revision is not None:
            _require_identifier("revision", self.revision)

    @property
    def key(self) -> str:
        return payload_sha256(self.to_payload())

    def to_payload(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "provider_id": self.provider_id,
            "revision": self.revision,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ModelCandidate:
        if set(payload) != {"provider_id", "model_id", "revision"}:
            raise ValueError("invalid model candidate payload")
        revision = payload["revision"]
        if revision is not None and not isinstance(revision, str):
            raise TypeError("revision must be a string or null")
        return cls(
            provider_id=payload["provider_id"],
            model_id=payload["model_id"],
            revision=revision,
        )


@dataclass(frozen=True, slots=True)
class BenchmarkMetric:
    name: str
    direction: MetricDirection
    weight: float
    worst_value: float
    best_value: float

    def __post_init__(self) -> None:
        _require_identifier("metric name", self.name)
        if not isinstance(self.direction, MetricDirection):
            raise TypeError("direction must be MetricDirection")
        weight = _require_finite("weight", self.weight)
        worst = _require_finite("worst_value", self.worst_value)
        best = _require_finite("best_value", self.best_value)
        if weight <= 0:
            raise ValueError("weight must be greater than zero")
        if self.direction is MetricDirection.MAXIMIZE and best <= worst:
            raise ValueError("maximize metric requires best_value > worst_value")
        if self.direction is MetricDirection.MINIMIZE and best >= worst:
            raise ValueError("minimize metric requires best_value < worst_value")

    def to_payload(self) -> dict[str, Any]:
        return {
            "best_value": float(self.best_value),
            "direction": self.direction.value,
            "name": self.name,
            "weight": float(self.weight),
            "worst_value": float(self.worst_value),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> BenchmarkMetric:
        expected = {"name", "direction", "weight", "worst_value", "best_value"}
        if set(payload) != expected:
            raise ValueError("invalid benchmark metric payload")
        return cls(
            name=payload["name"],
            direction=MetricDirection(payload["direction"]),
            weight=payload["weight"],
            worst_value=payload["worst_value"],
            best_value=payload["best_value"],
        )


@dataclass(frozen=True, slots=True)
class BenchmarkSuite:
    suite_id: str
    version: str
    dataset_sha256: str
    metrics: tuple[BenchmarkMetric, ...]
    required_case_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_identifier("suite_id", self.suite_id)
        _require_identifier("version", self.version)
        _require_sha256("dataset_sha256", self.dataset_sha256)
        if not self.metrics:
            raise ValueError("benchmark suite requires at least one metric")
        metric_names = [metric.name for metric in self.metrics]
        if len(set(metric_names)) != len(metric_names):
            raise ValueError("benchmark metric names must be unique")
        if not self.required_case_ids:
            raise ValueError("benchmark suite requires at least one case")
        for case_id in self.required_case_ids:
            _require_identifier("case_id", case_id)
        if len(set(self.required_case_ids)) != len(self.required_case_ids):
            raise ValueError("required_case_ids must be unique")

    @property
    def key(self) -> str:
        return f"{self.suite_id}@{self.version}"

    def to_payload(self) -> dict[str, Any]:
        return {
            "dataset_sha256": self.dataset_sha256,
            "metrics": [metric.to_payload() for metric in self.metrics],
            "required_case_ids": list(self.required_case_ids),
            "suite_id": self.suite_id,
            "version": self.version,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> BenchmarkSuite:
        expected = {"suite_id", "version", "dataset_sha256", "metrics", "required_case_ids"}
        if set(payload) != expected:
            raise ValueError("invalid benchmark suite payload")
        metrics = payload["metrics"]
        cases = payload["required_case_ids"]
        if not isinstance(metrics, list) or not isinstance(cases, list):
            raise TypeError("suite metrics and required_case_ids must be lists")
        return cls(
            suite_id=payload["suite_id"],
            version=payload["version"],
            dataset_sha256=payload["dataset_sha256"],
            metrics=tuple(BenchmarkMetric.from_payload(item) for item in metrics),
            required_case_ids=tuple(cases),
        )


@dataclass(frozen=True, slots=True)
class MetricObservation:
    name: str
    value: float

    def __post_init__(self) -> None:
        _require_identifier("metric observation name", self.name)
        _require_finite("metric observation value", self.value)

    def to_payload(self) -> dict[str, Any]:
        return {"name": self.name, "value": float(self.value)}

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> MetricObservation:
        if set(payload) != {"name", "value"}:
            raise ValueError("invalid metric observation payload")
        return cls(name=payload["name"], value=payload["value"])


@dataclass(frozen=True, slots=True)
class BenchmarkObservation:
    observation_id: str
    run_id: str
    suite_id: str
    suite_version: str
    candidate: ModelCandidate
    case_id: str
    input_sha256: str
    output_sha256: str
    metrics: tuple[MetricObservation, ...]
    observed_at: datetime

    def __post_init__(self) -> None:
        for name, value in (
            ("observation_id", self.observation_id),
            ("run_id", self.run_id),
            ("suite_id", self.suite_id),
            ("suite_version", self.suite_version),
            ("case_id", self.case_id),
        ):
            _require_identifier(name, value)
        if not isinstance(self.candidate, ModelCandidate):
            raise TypeError("candidate must be ModelCandidate")
        _require_sha256("input_sha256", self.input_sha256)
        _require_sha256("output_sha256", self.output_sha256)
        if not self.metrics:
            raise ValueError("observation requires at least one metric")
        metric_names = [metric.name for metric in self.metrics]
        if len(set(metric_names)) != len(metric_names):
            raise ValueError("observation metric names must be unique")
        _require_aware_datetime("observed_at", self.observed_at)

    @property
    def suite_key(self) -> str:
        return f"{self.suite_id}@{self.suite_version}"

    def to_payload(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate.to_payload(),
            "case_id": self.case_id,
            "input_sha256": self.input_sha256,
            "metrics": [metric.to_payload() for metric in self.metrics],
            "observation_id": self.observation_id,
            "observed_at": self.observed_at.astimezone(UTC).isoformat(),
            "output_sha256": self.output_sha256,
            "run_id": self.run_id,
            "suite_id": self.suite_id,
            "suite_version": self.suite_version,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> BenchmarkObservation:
        expected = {
            "candidate",
            "case_id",
            "input_sha256",
            "metrics",
            "observation_id",
            "observed_at",
            "output_sha256",
            "run_id",
            "suite_id",
            "suite_version",
        }
        if set(payload) != expected:
            raise ValueError("invalid benchmark observation payload")
        metrics = payload["metrics"]
        if not isinstance(metrics, list):
            raise TypeError("observation metrics must be a list")
        return cls(
            observation_id=payload["observation_id"],
            run_id=payload["run_id"],
            suite_id=payload["suite_id"],
            suite_version=payload["suite_version"],
            candidate=ModelCandidate.from_payload(payload["candidate"]),
            case_id=payload["case_id"],
            input_sha256=payload["input_sha256"],
            output_sha256=payload["output_sha256"],
            metrics=tuple(MetricObservation.from_payload(item) for item in metrics),
            observed_at=datetime.fromisoformat(payload["observed_at"]),
        )


@dataclass(frozen=True, slots=True)
class CandidateScore:
    candidate: ModelCandidate
    score_micros: int
    metric_score_micros: tuple[tuple[str, int], ...]

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, ModelCandidate):
            raise TypeError("candidate must be ModelCandidate")
        if isinstance(self.score_micros, bool) or not isinstance(self.score_micros, int):
            raise TypeError("score_micros must be an integer")
        if not 0 <= self.score_micros <= 1_000_000:
            raise ValueError("score_micros must be in [0, 1000000]")
        names: list[str] = []
        for name, value in self.metric_score_micros:
            _require_identifier("metric score name", name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError("metric score must be an integer")
            if not 0 <= value <= 1_000_000:
                raise ValueError("metric score must be in [0, 1000000]")
            names.append(name)
        if len(set(names)) != len(names):
            raise ValueError("metric scores must be unique")

    def to_payload(self) -> dict[str, Any]:
        return {
            "candidate": self.candidate.to_payload(),
            "metric_score_micros": [
                {"name": name, "score_micros": score}
                for name, score in self.metric_score_micros
            ],
            "score_micros": self.score_micros,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> CandidateScore:
        expected = {"candidate", "score_micros", "metric_score_micros"}
        if set(payload) != expected:
            raise ValueError("invalid candidate score payload")
        metric_scores = payload["metric_score_micros"]
        if not isinstance(metric_scores, list):
            raise TypeError("metric_score_micros must be a list")
        parsed: list[tuple[str, int]] = []
        for item in metric_scores:
            if not isinstance(item, dict) or set(item) != {"name", "score_micros"}:
                raise ValueError("invalid metric score payload")
            parsed.append((item["name"], item["score_micros"]))
        return cls(
            candidate=ModelCandidate.from_payload(payload["candidate"]),
            score_micros=payload["score_micros"],
            metric_score_micros=tuple(parsed),
        )


@dataclass(frozen=True, slots=True)
class CandidateExclusion:
    candidate: ModelCandidate
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, ModelCandidate):
            raise TypeError("candidate must be ModelCandidate")
        _require_identifier("exclusion reason", self.reason)

    def to_payload(self) -> dict[str, Any]:
        return {"candidate": self.candidate.to_payload(), "reason": self.reason}

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> CandidateExclusion:
        if set(payload) != {"candidate", "reason"}:
            raise ValueError("invalid candidate exclusion payload")
        return cls(
            candidate=ModelCandidate.from_payload(payload["candidate"]),
            reason=payload["reason"],
        )


@dataclass(frozen=True, slots=True)
class BenchmarkRecommendation:
    recommendation_id: str
    run_id: str
    suite_id: str
    suite_version: str
    ranked_candidates: tuple[CandidateScore, ...]
    excluded_candidates: tuple[CandidateExclusion, ...]
    source_observation_sha256: tuple[str, ...]
    evidence_sha256: str
    created_at: datetime
    requires_human_review: bool = True
    promotion_allowed: bool = False

    def __post_init__(self) -> None:
        for name, value in (
            ("recommendation_id", self.recommendation_id),
            ("run_id", self.run_id),
            ("suite_id", self.suite_id),
            ("suite_version", self.suite_version),
        ):
            _require_identifier(name, value)
        if not self.ranked_candidates:
            raise ValueError("recommendation requires at least one ranked candidate")
        ranked_keys = [score.candidate.key for score in self.ranked_candidates]
        if len(set(ranked_keys)) != len(ranked_keys):
            raise ValueError("ranked candidates must be unique")
        excluded_keys = [item.candidate.key for item in self.excluded_candidates]
        if len(set(excluded_keys)) != len(excluded_keys):
            raise ValueError("excluded candidates must be unique")
        if set(ranked_keys) & set(excluded_keys):
            raise ValueError("a candidate cannot be both ranked and excluded")
        if not self.source_observation_sha256:
            raise ValueError("recommendation requires source observation evidence")
        for digest in self.source_observation_sha256:
            _require_sha256("source observation digest", digest)
        if tuple(sorted(self.source_observation_sha256)) != self.source_observation_sha256:
            raise ValueError("source observation digests must be sorted")
        if len(set(self.source_observation_sha256)) != len(self.source_observation_sha256):
            raise ValueError("source observation digests must be unique")
        _require_sha256("evidence_sha256", self.evidence_sha256)
        _require_aware_datetime("created_at", self.created_at)
        if self.requires_human_review is not True:
            raise ValueError("Model Engineering recommendations always require human review")
        if self.promotion_allowed is not False:
            raise ValueError("Model Engineering recommendations cannot authorize promotion")

    @property
    def suite_key(self) -> str:
        return f"{self.suite_id}@{self.suite_version}"

    @property
    def winner(self) -> CandidateScore:
        return self.ranked_candidates[0]

    def to_payload(self) -> dict[str, Any]:
        return {
            "created_at": self.created_at.astimezone(UTC).isoformat(),
            "evidence_sha256": self.evidence_sha256,
            "excluded_candidates": [item.to_payload() for item in self.excluded_candidates],
            "promotion_allowed": self.promotion_allowed,
            "ranked_candidates": [item.to_payload() for item in self.ranked_candidates],
            "recommendation_id": self.recommendation_id,
            "requires_human_review": self.requires_human_review,
            "run_id": self.run_id,
            "source_observation_sha256": list(self.source_observation_sha256),
            "suite_id": self.suite_id,
            "suite_version": self.suite_version,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> BenchmarkRecommendation:
        expected = {
            "created_at",
            "evidence_sha256",
            "excluded_candidates",
            "promotion_allowed",
            "ranked_candidates",
            "recommendation_id",
            "requires_human_review",
            "run_id",
            "source_observation_sha256",
            "suite_id",
            "suite_version",
        }
        if set(payload) != expected:
            raise ValueError("invalid benchmark recommendation payload")
        ranked = payload["ranked_candidates"]
        excluded = payload["excluded_candidates"]
        if not isinstance(ranked, list) or not isinstance(excluded, list):
            raise TypeError("recommendation candidate collections must be lists")
        return cls(
            recommendation_id=payload["recommendation_id"],
            run_id=payload["run_id"],
            suite_id=payload["suite_id"],
            suite_version=payload["suite_version"],
            ranked_candidates=tuple(CandidateScore.from_payload(item) for item in ranked),
            excluded_candidates=tuple(CandidateExclusion.from_payload(item) for item in excluded),
            source_observation_sha256=tuple(payload["source_observation_sha256"]),
            evidence_sha256=payload["evidence_sha256"],
            created_at=datetime.fromisoformat(payload["created_at"]),
            requires_human_review=payload["requires_human_review"],
            promotion_allowed=payload["promotion_allowed"],
        )
