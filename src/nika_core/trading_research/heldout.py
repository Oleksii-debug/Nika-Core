from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .contracts import CausalityViolation, Partition, TradingResearchError, require_aware_utc
from .dataset import ValidationReport

_HELDOUT_SCHEMA = "nika-trader-heldout-v1"


@dataclass(frozen=True, slots=True)
class ReplayDataQuality:
    duplicate_count: int
    conflict_count: int
    gap_count: int

    def __post_init__(self) -> None:
        counts = (self.duplicate_count, self.conflict_count, self.gap_count)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in counts
        ):
            raise TradingResearchError("data-quality counts must be non-negative integers")

    @classmethod
    def from_report(cls, report: ValidationReport) -> ReplayDataQuality:
        return cls(
            duplicate_count=len(report.duplicates),
            conflict_count=len(report.conflicts),
            gap_count=len(report.gaps),
        )

    @property
    def is_clean(self) -> bool:
        return self.duplicate_count == self.conflict_count == self.gap_count == 0


@dataclass(frozen=True, slots=True)
class PartitionWindow:
    partition: Partition
    start_at: datetime
    end_at: datetime

    def __post_init__(self) -> None:
        start_at = require_aware_utc(self.start_at, "start_at")
        end_at = require_aware_utc(self.end_at, "end_at")
        if end_at <= start_at:
            raise TradingResearchError("partition window must have positive duration")
        object.__setattr__(self, "start_at", start_at)
        object.__setattr__(self, "end_at", end_at)


@dataclass(frozen=True, slots=True)
class HeldOutProtocol:
    train: PartitionWindow
    validation: PartitionWindow
    test: PartitionWindow

    def __post_init__(self) -> None:
        if self.train.partition is not Partition.TRAIN:
            raise TradingResearchError("train window must use the train partition")
        if self.validation.partition is not Partition.VALIDATION:
            raise TradingResearchError("validation window must use the validation partition")
        if self.test.partition is not Partition.TEST:
            raise TradingResearchError("test window must use the test partition")
        if self.train.end_at > self.validation.start_at:
            raise CausalityViolation("train and validation windows overlap")
        if self.validation.end_at > self.test.start_at:
            raise CausalityViolation("validation and held-out test windows overlap")

    @property
    def fingerprint(self) -> str:
        payload = "|".join(
            (
                _HELDOUT_SCHEMA,
                self.train.partition.value,
                self.train.start_at.isoformat(),
                self.train.end_at.isoformat(),
                self.validation.partition.value,
                self.validation.start_at.isoformat(),
                self.validation.end_at.isoformat(),
                self.test.partition.value,
                self.test.start_at.isoformat(),
                self.test.end_at.isoformat(),
            )
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CandidateScore:
    strategy_id: str
    partition: Partition
    metric_name: str
    metric_value: Decimal | None
    dataset_semantic_hash: str
    data_quality: ReplayDataQuality
    universe_fingerprint: str
    fit_cutoff_at: datetime
    universe_cutoff_at: datetime
    evaluated_at: datetime

    def __post_init__(self) -> None:
        if not self.strategy_id.strip() or not self.metric_name.strip():
            raise TradingResearchError("candidate score requires strategy and metric identity")
        _require_digest(self.dataset_semantic_hash, "dataset_semantic_hash")
        _require_digest(self.universe_fingerprint, "universe_fingerprint")
        if self.metric_value is not None and not self.metric_value.is_finite():
            raise TradingResearchError("candidate metric must be finite when supplied")
        object.__setattr__(
            self,
            "fit_cutoff_at",
            require_aware_utc(self.fit_cutoff_at, "fit_cutoff_at"),
        )
        object.__setattr__(
            self,
            "universe_cutoff_at",
            require_aware_utc(self.universe_cutoff_at, "universe_cutoff_at"),
        )
        object.__setattr__(
            self,
            "evaluated_at",
            require_aware_utc(self.evaluated_at, "evaluated_at"),
        )


@dataclass(frozen=True, slots=True)
class SelectionDecision:
    strategy_id: str
    metric_name: str
    metric_value: Decimal
    dataset_semantic_hash: str
    data_quality: ReplayDataQuality
    universe_fingerprint: str
    universe_cutoff_at: datetime
    selected_at: datetime
    protocol_fingerprint: str
    higher_is_better: bool
    source_partition: Partition = Partition.VALIDATION

    def __post_init__(self) -> None:
        if self.source_partition is not Partition.VALIDATION:
            raise CausalityViolation("strategy selection must use validation results only")
        if not self.metric_value.is_finite():
            raise TradingResearchError("selection metric must be finite")
        _require_digest(self.dataset_semantic_hash, "dataset_semantic_hash")
        _require_digest(self.universe_fingerprint, "universe_fingerprint")
        _require_digest(self.protocol_fingerprint, "protocol_fingerprint")
        object.__setattr__(
            self,
            "universe_cutoff_at",
            require_aware_utc(self.universe_cutoff_at, "universe_cutoff_at"),
        )
        object.__setattr__(
            self,
            "selected_at",
            require_aware_utc(self.selected_at, "selected_at"),
        )


@dataclass(frozen=True, slots=True)
class PartitionResult:
    strategy_id: str
    partition: Partition
    metric_name: str
    metric_value: Decimal | None
    dataset_semantic_hash: str
    data_quality: ReplayDataQuality
    universe_fingerprint: str
    fit_cutoff_at: datetime
    universe_cutoff_at: datetime
    evaluated_at: datetime

    def __post_init__(self) -> None:
        if not self.strategy_id.strip() or not self.metric_name.strip():
            raise TradingResearchError("partition result requires strategy and metric identity")
        _require_digest(self.dataset_semantic_hash, "dataset_semantic_hash")
        _require_digest(self.universe_fingerprint, "universe_fingerprint")
        if self.metric_value is not None and not self.metric_value.is_finite():
            raise TradingResearchError("partition metric must be finite when supplied")
        object.__setattr__(
            self,
            "fit_cutoff_at",
            require_aware_utc(self.fit_cutoff_at, "fit_cutoff_at"),
        )
        object.__setattr__(
            self,
            "universe_cutoff_at",
            require_aware_utc(self.universe_cutoff_at, "universe_cutoff_at"),
        )
        object.__setattr__(
            self,
            "evaluated_at",
            require_aware_utc(self.evaluated_at, "evaluated_at"),
        )


@dataclass(frozen=True, slots=True)
class HeldOutAssessment:
    selection: SelectionDecision
    test_result: PartitionResult

    def require_promotion_metric(self) -> Decimal:
        if self.test_result.metric_value is None:
            raise TradingResearchError(
                "held-out metric is unavailable; promotion evidence is forbidden"
            )
        return self.test_result.metric_value


def select_validation_candidate(
    protocol: HeldOutProtocol,
    scores: tuple[CandidateScore, ...],
    *,
    selected_at: datetime,
    higher_is_better: bool = True,
) -> SelectionDecision:
    if not scores:
        raise TradingResearchError("at least one validation score is required")
    selected_at = require_aware_utc(selected_at, "selected_at")
    if selected_at < protocol.validation.end_at:
        raise CausalityViolation("strategy cannot be selected before validation finishes")
    if selected_at > protocol.test.start_at:
        raise CausalityViolation("strategy selection occurred after held-out test began")

    metric_name = scores[0].metric_name
    dataset_hash = scores[0].dataset_semantic_hash
    data_quality = scores[0].data_quality
    universe_fingerprint = scores[0].universe_fingerprint
    universe_cutoff_at = scores[0].universe_cutoff_at
    seen_strategies: set[str] = set()
    for score in scores:
        if score.partition is not Partition.VALIDATION:
            raise CausalityViolation("candidate selection may consume validation scores only")
        if score.metric_name != metric_name:
            raise TradingResearchError("candidate scores must use one metric")
        if score.dataset_semantic_hash != dataset_hash:
            raise TradingResearchError("candidate scores must use one dataset version")
        if score.data_quality != data_quality:
            raise TradingResearchError("candidate scores disagree on dataset quality evidence")
        if score.universe_fingerprint != universe_fingerprint:
            raise TradingResearchError("candidate scores must use one fixed universe")
        if score.universe_cutoff_at != universe_cutoff_at:
            raise TradingResearchError("candidate scores must use one universe cutoff")
        if score.strategy_id in seen_strategies:
            raise TradingResearchError("candidate strategy IDs must be unique")
        seen_strategies.add(score.strategy_id)
        if not score.data_quality.is_clean:
            raise TradingResearchError(
                "validation dataset has duplicate, conflicting, or missing-sequence evidence"
            )
        if score.metric_value is None:
            raise TradingResearchError("candidate score is missing its selection metric")
        if score.fit_cutoff_at > protocol.train.end_at:
            raise CausalityViolation("validation candidate was fit using validation/future data")
        if score.universe_cutoff_at > protocol.validation.start_at:
            raise CausalityViolation("validation universe uses future membership information")
        if score.evaluated_at < protocol.validation.end_at:
            raise CausalityViolation("validation metric was finalized before validation ended")
        if score.evaluated_at > selected_at:
            raise CausalityViolation("selection predates its validation metric evidence")

    values = tuple(score.metric_value for score in scores if score.metric_value is not None)
    best_value = max(values) if higher_is_better else min(values)
    best = min(
        (score for score in scores if score.metric_value == best_value),
        key=lambda score: score.strategy_id,
    )
    return SelectionDecision(
        strategy_id=best.strategy_id,
        metric_name=metric_name,
        metric_value=best_value,
        dataset_semantic_hash=dataset_hash,
        data_quality=data_quality,
        universe_fingerprint=universe_fingerprint,
        universe_cutoff_at=universe_cutoff_at,
        selected_at=selected_at,
        protocol_fingerprint=protocol.fingerprint,
        higher_is_better=higher_is_better,
    )


def bind_held_out_test(
    protocol: HeldOutProtocol,
    selection: SelectionDecision,
    result: PartitionResult,
) -> HeldOutAssessment:
    if selection.protocol_fingerprint != protocol.fingerprint:
        raise TradingResearchError("selection belongs to a different held-out protocol")
    if selection.selected_at > protocol.test.start_at:
        raise CausalityViolation("selection must predate held-out test start")
    if result.partition is not Partition.TEST:
        raise CausalityViolation("held-out assessment requires the test partition")
    if result.strategy_id != selection.strategy_id:
        raise CausalityViolation("held-out result does not match the selected strategy")
    if result.metric_name != selection.metric_name:
        raise TradingResearchError("held-out result uses a different metric")
    if result.dataset_semantic_hash != selection.dataset_semantic_hash:
        raise TradingResearchError("held-out result uses a different dataset version")
    if not result.data_quality.is_clean:
        raise TradingResearchError(
            "held-out dataset has duplicate, conflicting, or missing-sequence evidence"
        )
    if result.data_quality != selection.data_quality:
        raise TradingResearchError("held-out result changes dataset quality evidence")
    if result.universe_fingerprint != selection.universe_fingerprint:
        raise CausalityViolation("held-out result changes the fixed validation universe")
    if result.universe_cutoff_at != selection.universe_cutoff_at:
        raise CausalityViolation("held-out result changes the fixed universe cutoff")
    if result.fit_cutoff_at > protocol.test.start_at:
        raise CausalityViolation("held-out strategy fit includes test/future data")
    if result.universe_cutoff_at > protocol.test.start_at:
        raise CausalityViolation("held-out universe uses future membership information")
    if result.evaluated_at < protocol.test.end_at:
        raise CausalityViolation("held-out metric was finalized before the test window ended")
    return HeldOutAssessment(selection=selection, test_result=result)


def _require_digest(value: str, field_name: str) -> None:
    if len(value) != 64:
        raise TradingResearchError(f"{field_name} must be a SHA-256 hex digest")
    try:
        int(value, 16)
    except ValueError as exc:
        raise TradingResearchError(
            f"{field_name} must be a SHA-256 hex digest"
        ) from exc
