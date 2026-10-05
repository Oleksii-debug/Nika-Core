from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from math import isfinite


def _finite_number(value: object, field: str, *, non_negative: bool = False) -> float:
    """Admit finite numeric evidence without bool or text coercion."""
    if type(value) not in (int, float):
        raise ValueError(f"{field} must be a finite number")
    try:
        number = float(value)
    except (OverflowError, ValueError):
        raise ValueError(f"{field} must be a finite number") from None
    if not isfinite(number):
        raise ValueError(f"{field} must be a finite number")
    if non_negative and number < 0:
        raise ValueError(f"{field} must be non-negative")
    return number


class ExperimentStatus(StrEnum):
    DRAFT = "draft"
    RUNNING = "running"
    COMPLETED = "completed"
    PROMOTED = "promoted"
    ROLLED_BACK = "rolled_back"


class ArtifactKind(StrEnum):
    PROMPT = "prompt"
    STRATEGY = "strategy"
    CONFIG = "config"


@dataclass(frozen=True, slots=True)
class StrategyRef:
    candidate_id: str
    version: str
    artifact_kind: ArtifactKind
    artifact_ref: str
    permission_fingerprint: str

    def __post_init__(self) -> None:
        for value, name in (
            (self.candidate_id, "candidate_id"),
            (self.version, "version"),
            (self.artifact_ref, "artifact_ref"),
            (self.permission_fingerprint, "permission_fingerprint"),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be empty")


@dataclass(frozen=True, slots=True)
class ReplayCase:
    replay_id: str
    dataset_ref: str
    dataset_version: str

    def __post_init__(self) -> None:
        if not self.replay_id.strip() or not self.dataset_ref.strip() or not self.dataset_version.strip():
            raise ValueError("replay identity must be complete")


@dataclass(frozen=True, slots=True)
class MetricObservation:
    candidate_id: str
    replay_id: str
    metric: str
    value: float

    def __post_init__(self) -> None:
        if not self.candidate_id.strip() or not self.replay_id.strip() or not self.metric.strip():
            raise ValueError("metric observation identifiers must not be empty")
        object.__setattr__(self, "value", _finite_number(self.value, "metric observation"))


@dataclass(frozen=True, slots=True)
class MetricRule:
    metric: str
    higher_is_better: bool = True
    max_regression: float = 0.0

    def __post_init__(self) -> None:
        if not self.metric.strip():
            raise ValueError("metric must not be empty")
        if type(self.higher_is_better) is not bool:
            raise ValueError("higher_is_better must be boolean")
        regression = _finite_number(self.max_regression, "max_regression", non_negative=True)
        object.__setattr__(self, "max_regression", regression)


@dataclass(frozen=True, slots=True)
class PromotionPolicy:
    primary_metric: str
    minimum_improvement: float = 0.0
    minimum_replays: int = 1
    guardrails: tuple[MetricRule, ...] = ()
    primary_higher_is_better: bool = True

    def __post_init__(self) -> None:
        if not self.primary_metric.strip():
            raise ValueError("primary_metric must not be empty")
        improvement = _finite_number(
            self.minimum_improvement, "minimum_improvement", non_negative=True
        )
        object.__setattr__(self, "minimum_improvement", improvement)
        if type(self.minimum_replays) is not int or self.minimum_replays < 1:
            raise ValueError("minimum_replays must be a positive integer")
        if type(self.primary_higher_is_better) is not bool:
            raise ValueError("primary_higher_is_better must be boolean")
        names = [rule.metric for rule in self.guardrails]
        if len(names) != len(set(names)):
            raise ValueError("guardrail metrics must be unique")
        if self.primary_metric in names:
            raise ValueError("primary metric must not be duplicated as a guardrail")


@dataclass(frozen=True, slots=True)
class ExperimentDefinition:
    experiment_id: str
    champion: StrategyRef
    challengers: tuple[StrategyRef, ...]
    replays: tuple[ReplayCase, ...]
    policy: PromotionPolicy

    def __post_init__(self) -> None:
        if not self.experiment_id.strip():
            raise ValueError("experiment_id must not be empty")
        if not self.challengers:
            raise ValueError("at least one challenger is required")
        if not self.replays:
            raise ValueError("at least one replay is required")
        if len(self.replays) < self.policy.minimum_replays:
            raise ValueError("declared replay set is smaller than minimum_replays")
        candidates = (self.champion, *self.challengers)
        ids = [candidate.candidate_id for candidate in candidates]
        if len(ids) != len(set(ids)):
            raise ValueError("candidate IDs must be unique")
        replay_ids = [replay.replay_id for replay in self.replays]
        if len(replay_ids) != len(set(replay_ids)):
            raise ValueError("replay IDs must be unique")
        permission_truth = self.champion.permission_fingerprint
        if any(candidate.permission_fingerprint != permission_truth for candidate in self.challengers):
            raise PermissionError("experiment candidates may not widen or alter permissions")


@dataclass(frozen=True, slots=True)
class ExperimentSnapshot:
    definition: ExperimentDefinition
    status: ExperimentStatus = ExperimentStatus.DRAFT
    observations: tuple[MetricObservation, ...] = ()
    selected_candidate_id: str | None = None
    previous_champion_id: str | None = None
