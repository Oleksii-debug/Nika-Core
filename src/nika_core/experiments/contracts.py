from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from math import isfinite


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


class DatasetSplit(StrEnum):
    TRAINING = "training"
    EVALUATION = "evaluation"
    HELD_OUT = "held_out"


def _require_text(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if not value.strip():
        raise ValueError(f"{name} must not be empty")
    return value


def _require_number(value: object, name: str) -> int | float:
    if type(value) not in (int, float):
        raise TypeError(f"{name} must be numeric")
    if not isfinite(float(value)):
        raise ValueError(f"{name} must be finite")
    return value


@dataclass(frozen=True, slots=True)
class StrategyRef:
    candidate_id: str
    version: str
    artifact_kind: ArtifactKind
    artifact_ref: str
    permission_fingerprint: str
    training_dataset_fingerprints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for value, name in (
            (self.candidate_id, "candidate_id"),
            (self.version, "version"),
            (self.artifact_ref, "artifact_ref"),
            (self.permission_fingerprint, "permission_fingerprint"),
        ):
            _require_text(value, name)
        if not isinstance(self.artifact_kind, ArtifactKind):
            raise TypeError("artifact_kind must be an ArtifactKind")
        fingerprints = self.training_dataset_fingerprints
        if not isinstance(fingerprints, tuple):
            raise TypeError("training dataset fingerprints must be a tuple")
        if any(not isinstance(item, str) for item in fingerprints):
            raise TypeError("training dataset fingerprints must be strings")
        if any(not item.strip() for item in fingerprints):
            raise ValueError("training dataset fingerprints must not be empty")
        if any(item != item.strip() for item in fingerprints):
            raise ValueError("training dataset fingerprints must be canonical without whitespace")
        if len(fingerprints) != len(set(fingerprints)):
            raise ValueError("training dataset fingerprints must be unique")


@dataclass(frozen=True, slots=True)
class ReplayCase:
    replay_id: str
    dataset_ref: str
    dataset_version: str
    split: DatasetSplit = DatasetSplit.EVALUATION
    dataset_fingerprint: str | None = None
    data_end_at: datetime | None = None

    def __post_init__(self) -> None:
        _require_text(self.replay_id, "replay_id")
        _require_text(self.dataset_ref, "dataset_ref")
        _require_text(self.dataset_version, "dataset_version")
        if not isinstance(self.split, DatasetSplit):
            raise TypeError("replay split must be a DatasetSplit")
        if self.split is DatasetSplit.TRAINING:
            raise ValueError("promotion replay cannot use the training split")
        if self.dataset_fingerprint is not None:
            if not isinstance(self.dataset_fingerprint, str):
                raise TypeError("dataset_fingerprint must be a string when provided")
            if not self.dataset_fingerprint.strip():
                raise ValueError("dataset_fingerprint must not be empty when provided")
            if self.dataset_fingerprint != self.dataset_fingerprint.strip():
                raise ValueError("dataset_fingerprint must be canonical without whitespace")
        if self.data_end_at is not None:
            if not isinstance(self.data_end_at, datetime):
                raise TypeError("data_end_at must be a datetime when provided")
            if self.data_end_at.tzinfo is None:
                raise ValueError("data_end_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class MetricObservation:
    candidate_id: str
    replay_id: str
    metric: str
    value: float

    def __post_init__(self) -> None:
        _require_text(self.candidate_id, "candidate_id")
        _require_text(self.replay_id, "replay_id")
        _require_text(self.metric, "metric")
        _require_number(self.value, "metric observation value")


@dataclass(frozen=True, slots=True)
class MetricRule:
    metric: str
    higher_is_better: bool = True
    max_regression: float = 0.0

    def __post_init__(self) -> None:
        _require_text(self.metric, "metric")
        if type(self.higher_is_better) is not bool:
            raise TypeError("higher_is_better must be a boolean")
        _require_number(self.max_regression, "max_regression")
        if self.max_regression < 0:
            raise ValueError("max_regression must be non-negative")


@dataclass(frozen=True, slots=True)
class PromotionPolicy:
    primary_metric: str
    minimum_improvement: float = 0.0
    minimum_replays: int = 1
    guardrails: tuple[MetricRule, ...] = ()
    primary_higher_is_better: bool = True

    def __post_init__(self) -> None:
        _require_text(self.primary_metric, "primary_metric")
        _require_number(self.minimum_improvement, "minimum_improvement")
        if self.minimum_improvement < 0:
            raise ValueError("minimum_improvement must be non-negative")
        if type(self.minimum_replays) is not int:
            raise TypeError("minimum_replays must be an integer")
        if self.minimum_replays < 1:
            raise ValueError("minimum_replays must be at least 1")
        if not isinstance(self.guardrails, tuple):
            raise TypeError("guardrails must be a tuple")
        if any(not isinstance(rule, MetricRule) for rule in self.guardrails):
            raise TypeError("guardrails must contain MetricRule values")
        if type(self.primary_higher_is_better) is not bool:
            raise TypeError("primary_higher_is_better must be a boolean")
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
    evaluation_cutoff: datetime | None = None

    def __post_init__(self) -> None:
        _require_text(self.experiment_id, "experiment_id")
        if not isinstance(self.champion, StrategyRef):
            raise TypeError("champion must be a StrategyRef")
        if not isinstance(self.challengers, tuple) or any(
            not isinstance(item, StrategyRef) for item in self.challengers
        ):
            raise TypeError("challengers must be a tuple of StrategyRef values")
        if not isinstance(self.replays, tuple) or any(
            not isinstance(item, ReplayCase) for item in self.replays
        ):
            raise TypeError("replays must be a tuple of ReplayCase values")
        if not isinstance(self.policy, PromotionPolicy):
            raise TypeError("policy must be a PromotionPolicy")
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
        if any(
            candidate.permission_fingerprint != permission_truth
            for candidate in self.challengers
        ):
            raise PermissionError("experiment candidates may not widen or alter permissions")
        if self.evaluation_cutoff is not None:
            if not isinstance(self.evaluation_cutoff, datetime):
                raise TypeError("evaluation_cutoff must be a datetime when provided")
            if self.evaluation_cutoff.tzinfo is None:
                raise ValueError("evaluation_cutoff must be timezone-aware")

        training_fingerprints = {
            fingerprint
            for candidate in candidates
            for fingerprint in candidate.training_dataset_fingerprints
        }
        if training_fingerprints:
            missing = [
                replay.replay_id for replay in self.replays if replay.dataset_fingerprint is None
            ]
            if missing:
                raise ValueError(
                    "evaluation replay fingerprints are required when candidate training data "
                    "is declared"
                )
            overlap = sorted(
                {
                    replay.dataset_fingerprint
                    for replay in self.replays
                    if replay.dataset_fingerprint in training_fingerprints
                }
            )
            if overlap:
                raise ValueError("evaluation data overlaps candidate training data")

        if self.evaluation_cutoff is not None:
            missing_cutoff = [
                replay.replay_id for replay in self.replays if replay.data_end_at is None
            ]
            if missing_cutoff:
                raise ValueError(
                    "replay data_end_at is required when an evaluation cutoff is declared"
                )
            future = [
                replay.replay_id
                for replay in self.replays
                if replay.data_end_at is not None and replay.data_end_at > self.evaluation_cutoff
            ]
            if future:
                raise ValueError("evaluation replay contains data after the declared cutoff")


@dataclass(frozen=True, slots=True)
class ExperimentSnapshot:
    definition: ExperimentDefinition
    status: ExperimentStatus = ExperimentStatus.DRAFT
    observations: tuple[MetricObservation, ...] = ()
    selected_candidate_id: str | None = None
    previous_champion_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.definition, ExperimentDefinition):
            raise TypeError("definition must be an ExperimentDefinition")
        if not isinstance(self.status, ExperimentStatus):
            raise TypeError("status must be an ExperimentStatus")
        if not isinstance(self.observations, tuple) or any(
            not isinstance(item, MetricObservation) for item in self.observations
        ):
            raise TypeError("observations must be a tuple of MetricObservation values")
        for value, name in (
            (self.selected_candidate_id, "selected_candidate_id"),
            (self.previous_champion_id, "previous_champion_id"),
        ):
            if value is not None:
                _require_text(value, name)
