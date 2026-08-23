from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from .contracts import CausalityViolation, Partition, TradingResearchError, require_aware_utc
from .dataset import ValidationReport

_HELDOUT_SCHEMA = "nika-trader-heldout-v2"
_STRATEGY_ARTIFACT_SCHEMA = "nika-trader-strategy-artifact-v1"
_QUALITY_SCHEMA = "nika-trader-data-quality-v1"
_CANDIDATE_SCHEMA = "nika-trader-candidate-score-v1"
_SELECTION_SCHEMA = "nika-trader-selection-v1"
_RESULT_SCHEMA = "nika-trader-partition-result-v1"
_ASSESSMENT_SCHEMA = "nika-trader-heldout-assessment-v1"


class RefitPolicy(StrEnum):
    NO_REFIT = "no_refit"
    REFIT_TRAIN_VALIDATION = "refit_train_validation"


@dataclass(frozen=True, slots=True)
class ReplayDataQuality:
    duplicate_count: int
    conflict_count: int
    gap_count: int
    evidence_sha256: str

    def __post_init__(self) -> None:
        counts = (self.duplicate_count, self.conflict_count, self.gap_count)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in counts
        ):
            raise TradingResearchError("data-quality counts must be non-negative integers")
        _require_digest(self.evidence_sha256, "data-quality evidence_sha256")

    @classmethod
    def from_report(cls, report: ValidationReport) -> ReplayDataQuality:
        if not isinstance(report, ValidationReport):
            raise TradingResearchError("report must be ValidationReport evidence")
        payload = {
            "schema": _QUALITY_SCHEMA,
            "duplicates": _quality_issues(report.duplicates),
            "conflicts": _quality_issues(report.conflicts),
            "gaps": _quality_issues(report.gaps),
        }
        evidence_sha256 = hashlib.sha256(
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        return cls(
            duplicate_count=len(report.duplicates),
            conflict_count=len(report.conflicts),
            gap_count=len(report.gaps),
            evidence_sha256=evidence_sha256,
        )

    @property
    def is_clean(self) -> bool:
        return self.duplicate_count == self.conflict_count == self.gap_count == 0


def _quality_issues(issues: tuple[object, ...]) -> list[dict[str, object]]:
    return [
        {
            "code": issue.code,
            "message": issue.message,
            "event_indexes": list(issue.event_indexes),
        }
        for issue in issues
    ]


@dataclass(frozen=True, slots=True)
class StrategyArtifactFingerprint:
    strategy_id: str
    strategy_version: str
    algorithm_sha256: str
    config_sha256: str
    feature_pipeline_sha256: str
    fitted_state_sha256: str
    seed: int
    fit_cutoff_at: datetime
    created_at: datetime

    def __post_init__(self) -> None:
        _require_identity(self.strategy_id, "strategy_id")
        _require_identity(self.strategy_version, "strategy_version")
        _require_digest(self.algorithm_sha256, "algorithm_sha256")
        _require_digest(self.config_sha256, "config_sha256")
        _require_digest(self.feature_pipeline_sha256, "feature_pipeline_sha256")
        _require_digest(self.fitted_state_sha256, "fitted_state_sha256")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int) or self.seed < 0:
            raise TradingResearchError("strategy seed must be a non-negative integer")
        fit_cutoff_at = _require_aware_utc(self.fit_cutoff_at, "fit_cutoff_at")
        created_at = _require_aware_utc(self.created_at, "created_at")
        if created_at < fit_cutoff_at:
            raise CausalityViolation("strategy artifact cannot predate its fit cutoff")
        object.__setattr__(self, "fit_cutoff_at", fit_cutoff_at)
        object.__setattr__(self, "created_at", created_at)

    @property
    def fingerprint(self) -> str:
        payload = "|".join(
            (
                _STRATEGY_ARTIFACT_SCHEMA,
                self.strategy_id,
                self.strategy_version,
                self.algorithm_sha256,
                self.config_sha256,
                self.feature_pipeline_sha256,
                self.fitted_state_sha256,
                str(self.seed),
                self.fit_cutoff_at.isoformat(),
                self.created_at.isoformat(),
            )
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def same_definition_as(self, other: StrategyArtifactFingerprint) -> bool:
        if not isinstance(other, StrategyArtifactFingerprint):
            return False
        return (
            self.strategy_id == other.strategy_id
            and self.strategy_version == other.strategy_version
            and self.algorithm_sha256 == other.algorithm_sha256
            and self.config_sha256 == other.config_sha256
            and self.feature_pipeline_sha256 == other.feature_pipeline_sha256
            and self.seed == other.seed
        )


@dataclass(frozen=True, slots=True)
class PartitionWindow:
    partition: Partition
    start_at: datetime
    end_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.partition, Partition):
            raise TradingResearchError("partition window must use a Partition value")
        start_at = _require_aware_utc(self.start_at, "start_at")
        end_at = _require_aware_utc(self.end_at, "end_at")
        if end_at <= start_at:
            raise TradingResearchError("partition window must have positive duration")
        object.__setattr__(self, "start_at", start_at)
        object.__setattr__(self, "end_at", end_at)


@dataclass(frozen=True, slots=True)
class HeldOutProtocol:
    train: PartitionWindow
    validation: PartitionWindow
    test: PartitionWindow
    refit_policy: RefitPolicy = RefitPolicy.NO_REFIT
    _sealed_fingerprint: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.refit_policy, RefitPolicy):
            raise TradingResearchError("refit_policy must be a RefitPolicy")
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
        object.__setattr__(self, "_sealed_fingerprint", _protocol_fingerprint(self))

    @property
    def fingerprint(self) -> str:
        return _protocol_fingerprint(self)


@dataclass(frozen=True, slots=True)
class CandidateScore:
    strategy_artifact: StrategyArtifactFingerprint
    partition: Partition
    metric_name: str
    metric_fingerprint: str
    metric_value: Decimal | None
    dataset_semantic_hash: str
    data_quality: ReplayDataQuality
    universe_fingerprint: str
    universe_cutoff_at: datetime
    evaluated_at: datetime
    _artifact_fingerprint: str = field(init=False, repr=False)
    _evidence_fingerprint: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        artifact = _validated_strategy_artifact(self.strategy_artifact)
        if not isinstance(self.partition, Partition):
            raise TradingResearchError("candidate partition must be a Partition")
        _require_identity(self.metric_name, "metric_name")
        _require_digest(self.metric_fingerprint, "metric_fingerprint")
        _require_metric(self.metric_value, "candidate metric", allow_none=True)
        _require_digest(self.dataset_semantic_hash, "dataset_semantic_hash")
        _require_data_quality(self.data_quality)
        _require_digest(self.universe_fingerprint, "universe_fingerprint")
        universe_cutoff_at = _require_aware_utc(
            self.universe_cutoff_at,
            "universe_cutoff_at",
        )
        evaluated_at = _require_aware_utc(self.evaluated_at, "evaluated_at")
        object.__setattr__(self, "strategy_artifact", artifact)
        object.__setattr__(self, "universe_cutoff_at", universe_cutoff_at)
        object.__setattr__(self, "evaluated_at", evaluated_at)
        object.__setattr__(self, "_artifact_fingerprint", artifact.fingerprint)
        object.__setattr__(self, "_evidence_fingerprint", _candidate_fingerprint(self))

    @property
    def strategy_id(self) -> str:
        return self.strategy_artifact.strategy_id

    @property
    def fit_cutoff_at(self) -> datetime:
        return self.strategy_artifact.fit_cutoff_at


@dataclass(frozen=True, slots=True, init=False)
class SelectionDecision:
    strategy_artifact: StrategyArtifactFingerprint
    strategy_artifact_fingerprint: str
    metric_name: str
    metric_fingerprint: str
    metric_value: Decimal
    dataset_semantic_hash: str
    data_quality: ReplayDataQuality
    universe_fingerprint: str
    universe_cutoff_at: datetime
    selected_at: datetime
    protocol_fingerprint: str
    higher_is_better: bool
    source_partition: Partition = Partition.VALIDATION
    _evidence_fingerprint: str = field(init=False, repr=False)

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError(
            "SelectionDecision instances are created only by select_validation_candidate()"
        )

    @classmethod
    def _create(
        cls,
        *,
        strategy_artifact: StrategyArtifactFingerprint,
        metric_name: str,
        metric_fingerprint: str,
        metric_value: Decimal,
        dataset_semantic_hash: str,
        data_quality: ReplayDataQuality,
        universe_fingerprint: str,
        universe_cutoff_at: datetime,
        selected_at: datetime,
        protocol_fingerprint: str,
        higher_is_better: bool,
    ) -> SelectionDecision:
        artifact = _validated_strategy_artifact(strategy_artifact)
        obj = object.__new__(cls)
        values = {
            "strategy_artifact": artifact,
            "strategy_artifact_fingerprint": artifact.fingerprint,
            "metric_name": metric_name,
            "metric_fingerprint": metric_fingerprint,
            "metric_value": metric_value,
            "dataset_semantic_hash": dataset_semantic_hash,
            "data_quality": _copy_quality(data_quality),
            "universe_fingerprint": universe_fingerprint,
            "universe_cutoff_at": _require_aware_utc(
                universe_cutoff_at,
                "universe_cutoff_at",
            ),
            "selected_at": _require_aware_utc(selected_at, "selected_at"),
            "protocol_fingerprint": protocol_fingerprint,
            "higher_is_better": higher_is_better,
            "source_partition": Partition.VALIDATION,
        }
        for name, value in values.items():
            object.__setattr__(obj, name, value)
        object.__setattr__(obj, "_evidence_fingerprint", _selection_fingerprint(obj))
        _validate_selection_identity(obj)
        return obj

    @property
    def strategy_id(self) -> str:
        return self.strategy_artifact.strategy_id


@dataclass(frozen=True, slots=True)
class PartitionResult:
    strategy_artifact: StrategyArtifactFingerprint
    partition: Partition
    metric_name: str
    metric_fingerprint: str
    metric_value: Decimal | None
    dataset_semantic_hash: str
    data_quality: ReplayDataQuality
    universe_fingerprint: str
    universe_cutoff_at: datetime
    evaluated_at: datetime
    _artifact_fingerprint: str = field(init=False, repr=False)
    _evidence_fingerprint: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        artifact = _validated_strategy_artifact(self.strategy_artifact)
        if not isinstance(self.partition, Partition):
            raise TradingResearchError("result partition must be a Partition")
        _require_identity(self.metric_name, "metric_name")
        _require_digest(self.metric_fingerprint, "metric_fingerprint")
        _require_metric(self.metric_value, "partition metric", allow_none=True)
        _require_digest(self.dataset_semantic_hash, "dataset_semantic_hash")
        _require_data_quality(self.data_quality)
        _require_digest(self.universe_fingerprint, "universe_fingerprint")
        universe_cutoff_at = _require_aware_utc(
            self.universe_cutoff_at,
            "universe_cutoff_at",
        )
        evaluated_at = _require_aware_utc(self.evaluated_at, "evaluated_at")
        object.__setattr__(self, "strategy_artifact", artifact)
        object.__setattr__(self, "universe_cutoff_at", universe_cutoff_at)
        object.__setattr__(self, "evaluated_at", evaluated_at)
        object.__setattr__(self, "_artifact_fingerprint", artifact.fingerprint)
        object.__setattr__(self, "_evidence_fingerprint", _result_fingerprint(self))

    @property
    def strategy_id(self) -> str:
        return self.strategy_artifact.strategy_id

    @property
    def fit_cutoff_at(self) -> datetime:
        return self.strategy_artifact.fit_cutoff_at


@dataclass(frozen=True, slots=True, init=False)
class HeldOutAssessment:
    protocol: HeldOutProtocol
    selection: SelectionDecision
    test_result: PartitionResult
    _authority_fingerprint: str

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError(
            "HeldOutAssessment instances are created only by bind_held_out_test()"
        )

    @classmethod
    def _create(
        cls,
        protocol: HeldOutProtocol,
        selection: SelectionDecision,
        test_result: PartitionResult,
    ) -> HeldOutAssessment:
        protocol_snapshot = _validated_protocol(protocol)
        selection_snapshot = _copy_selection(selection)
        result_snapshot = _copy_result(test_result)
        obj = object.__new__(cls)
        object.__setattr__(obj, "protocol", protocol_snapshot)
        object.__setattr__(obj, "selection", selection_snapshot)
        object.__setattr__(obj, "test_result", result_snapshot)
        object.__setattr__(
            obj,
            "_authority_fingerprint",
            _assessment_fingerprint(
                protocol_snapshot,
                selection_snapshot,
                result_snapshot,
            ),
        )
        _validate_assessment_identity(obj)
        return obj

    def require_promotion_metric(self) -> Decimal:
        _validate_assessment_identity(self)
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
    validated_protocol = _validated_protocol(protocol)
    if not scores:
        raise TradingResearchError("at least one validation score is required")
    if not isinstance(higher_is_better, bool):
        raise TradingResearchError("higher_is_better must be a boolean")
    selected_at = _require_aware_utc(selected_at, "selected_at")
    if selected_at < validated_protocol.validation.end_at:
        raise CausalityViolation("strategy cannot be selected before validation finishes")
    if selected_at > validated_protocol.test.start_at:
        raise CausalityViolation("strategy selection occurred after held-out test began")

    first = _validate_candidate_score(scores[0])
    metric_name = first.metric_name
    metric_fingerprint = first.metric_fingerprint
    dataset_hash = first.dataset_semantic_hash
    data_quality = first.data_quality
    universe_fingerprint = first.universe_fingerprint
    universe_cutoff_at = first.universe_cutoff_at
    seen_strategies: set[str] = set()

    validated_scores: list[CandidateScore] = []
    for score in scores:
        score = _validate_candidate_score(score)
        validated_scores.append(score)
        if score.partition is not Partition.VALIDATION:
            raise CausalityViolation("candidate selection may consume validation scores only")
        if score.metric_name != metric_name:
            raise TradingResearchError("candidate scores must use one metric name")
        if score.metric_fingerprint != metric_fingerprint:
            raise TradingResearchError("candidate scores must use one metric definition")
        if score.dataset_semantic_hash != dataset_hash:
            raise TradingResearchError("candidate scores must use one dataset version")
        if score.data_quality != data_quality:
            raise TradingResearchError(
                "candidate scores disagree on exact dataset quality evidence"
            )
        if score.universe_fingerprint != universe_fingerprint:
            raise TradingResearchError("candidate scores must use one fixed universe")
        if score.universe_cutoff_at != universe_cutoff_at:
            raise TradingResearchError("candidate scores must use one universe cutoff")
        if score.strategy_id in seen_strategies:
            raise TradingResearchError("candidate strategy IDs must be unique")
        seen_strategies.add(score.strategy_id)
        if not score.data_quality.is_clean:
            raise TradingResearchError(
                "validation dataset has duplicate, conflicting, or gap evidence"
            )
        if score.metric_value is None:
            raise TradingResearchError("candidate score is missing its selection metric")
        if score.fit_cutoff_at > validated_protocol.train.end_at:
            raise CausalityViolation("validation candidate was fit using validation/future data")
        if score.strategy_artifact.created_at > validated_protocol.validation.start_at:
            raise CausalityViolation(
                "validation strategy artifact was created after validation began"
            )
        if score.universe_cutoff_at >= validated_protocol.validation.start_at:
            raise CausalityViolation("validation universe must be fixed before validation starts")
        if score.evaluated_at < validated_protocol.validation.end_at:
            raise CausalityViolation("validation metric was finalized before validation ended")
        if score.evaluated_at > selected_at:
            raise CausalityViolation("selection predates its validation metric evidence")

    values = tuple(
        score.metric_value
        for score in validated_scores
        if score.metric_value is not None
    )
    best_value = max(values) if higher_is_better else min(values)
    best = min(
        (score for score in validated_scores if score.metric_value == best_value),
        key=lambda score: score.strategy_id,
    )
    return SelectionDecision._create(
        strategy_artifact=best.strategy_artifact,
        metric_name=metric_name,
        metric_fingerprint=metric_fingerprint,
        metric_value=best_value,
        dataset_semantic_hash=dataset_hash,
        data_quality=data_quality,
        universe_fingerprint=universe_fingerprint,
        universe_cutoff_at=universe_cutoff_at,
        selected_at=selected_at,
        protocol_fingerprint=validated_protocol.fingerprint,
        higher_is_better=higher_is_better,
    )


def bind_held_out_test(
    protocol: HeldOutProtocol,
    selection: SelectionDecision,
    result: PartitionResult,
) -> HeldOutAssessment:
    validated_protocol = _validated_protocol(protocol)
    selected_at, universe_cutoff_at = _validate_selection_identity(selection)
    _validate_partition_result_identity(result)

    if selection.protocol_fingerprint != validated_protocol.fingerprint:
        raise TradingResearchError("selection belongs to a different held-out protocol")
    if selected_at < validated_protocol.validation.end_at:
        raise CausalityViolation("selection predates validation completion")
    if selected_at > validated_protocol.test.start_at:
        raise CausalityViolation("selection occurred after held-out test began")
    if universe_cutoff_at >= validated_protocol.validation.start_at:
        raise CausalityViolation("selection universe was not fixed before validation")
    _validate_test_identity(validated_protocol, selection, result)
    _validate_refit(validated_protocol, selection, result)
    return HeldOutAssessment._create(validated_protocol, selection, result)


def _validate_test_identity(
    protocol: HeldOutProtocol,
    selection: SelectionDecision,
    result: PartitionResult,
) -> None:
    if result.partition is not Partition.TEST:
        raise CausalityViolation("held-out assessment requires the test partition")
    if result.strategy_id != selection.strategy_id:
        raise CausalityViolation("held-out result does not match the selected strategy")
    if result.metric_name != selection.metric_name:
        raise TradingResearchError("held-out result uses a different metric name")
    if result.metric_fingerprint != selection.metric_fingerprint:
        raise TradingResearchError("held-out result uses a different metric definition")
    if result.dataset_semantic_hash != selection.dataset_semantic_hash:
        raise TradingResearchError("held-out result uses a different dataset version")
    if not result.data_quality.is_clean:
        raise TradingResearchError(
            "held-out dataset has duplicate, conflicting, or gap evidence"
        )
    if result.data_quality != selection.data_quality:
        raise TradingResearchError("held-out result changes exact dataset quality evidence")
    if result.universe_fingerprint != selection.universe_fingerprint:
        raise CausalityViolation("held-out result changes the fixed validation universe")
    if result.universe_cutoff_at != selection.universe_cutoff_at:
        raise CausalityViolation("held-out result changes the fixed universe cutoff")
    if result.universe_cutoff_at >= protocol.validation.start_at:
        raise CausalityViolation("held-out universe was not fixed before validation")
    if result.evaluated_at < protocol.test.end_at:
        raise CausalityViolation("held-out metric was finalized before the test window ended")


def _validate_refit(
    protocol: HeldOutProtocol,
    selection: SelectionDecision,
    result: PartitionResult,
) -> None:
    selected_artifact = _validated_strategy_artifact(selection.strategy_artifact)
    test_artifact = _validated_strategy_artifact(result.strategy_artifact)
    if not selected_artifact.same_definition_as(test_artifact):
        raise CausalityViolation("held-out strategy definition differs from validation selection")
    if test_artifact.fit_cutoff_at > protocol.validation.end_at:
        raise CausalityViolation("held-out strategy fit includes test/future data")
    if test_artifact.created_at > protocol.test.start_at:
        raise CausalityViolation("held-out strategy artifact was created after test start")

    if protocol.refit_policy is RefitPolicy.NO_REFIT:
        if test_artifact.fingerprint != selection.strategy_artifact_fingerprint:
            raise CausalityViolation("NO_REFIT requires the exact selected fitted artifact")
        return

    if test_artifact.fit_cutoff_at != protocol.validation.end_at:
        raise CausalityViolation(
            "REFIT_TRAIN_VALIDATION requires a fit cutoff at validation end"
        )
    if test_artifact.created_at < selection.selected_at:
        raise CausalityViolation("held-out refit artifact predates strategy selection")


def _validate_assessment_identity(assessment: HeldOutAssessment) -> None:
    if not isinstance(assessment, HeldOutAssessment):
        raise TradingResearchError("assessment must be HeldOutAssessment evidence")
    protocol = _validated_protocol(assessment.protocol)
    expected = _assessment_fingerprint(
        protocol,
        assessment.selection,
        assessment.test_result,
    )
    if expected != assessment._authority_fingerprint:
        raise TradingResearchError("held-out assessment evidence changed after binding")
    _validate_selection_identity(assessment.selection)
    _validate_partition_result_identity(assessment.test_result)
    if assessment.selection.protocol_fingerprint != protocol.fingerprint:
        raise TradingResearchError("held-out assessment protocol identity changed")
    if assessment.selection.selected_at < protocol.validation.end_at:
        raise CausalityViolation("held-out assessment selection predates validation completion")
    if assessment.selection.selected_at > protocol.test.start_at:
        raise CausalityViolation("held-out assessment selection follows held-out test start")
    if assessment.selection.universe_cutoff_at >= protocol.validation.start_at:
        raise CausalityViolation("held-out assessment universe was not fixed before validation")
    _validate_test_identity(protocol, assessment.selection, assessment.test_result)
    _validate_refit(protocol, assessment.selection, assessment.test_result)


def _validated_protocol(protocol: HeldOutProtocol) -> HeldOutProtocol:
    if not isinstance(protocol, HeldOutProtocol):
        raise TradingResearchError("protocol must be HeldOutProtocol evidence")
    windows = (protocol.train, protocol.validation, protocol.test)
    if any(not isinstance(window, PartitionWindow) for window in windows):
        raise TradingResearchError("protocol windows must be PartitionWindow evidence")
    if _protocol_fingerprint(protocol) != protocol._sealed_fingerprint:
        raise TradingResearchError("held-out protocol evidence changed after construction")
    return HeldOutProtocol(
        PartitionWindow(
            protocol.train.partition,
            protocol.train.start_at,
            protocol.train.end_at,
        ),
        PartitionWindow(
            protocol.validation.partition,
            protocol.validation.start_at,
            protocol.validation.end_at,
        ),
        PartitionWindow(
            protocol.test.partition,
            protocol.test.start_at,
            protocol.test.end_at,
        ),
        protocol.refit_policy,
    )


def _validated_strategy_artifact(
    artifact: StrategyArtifactFingerprint,
) -> StrategyArtifactFingerprint:
    if not isinstance(artifact, StrategyArtifactFingerprint):
        raise TradingResearchError(
            "strategy_artifact must be StrategyArtifactFingerprint evidence"
        )
    return StrategyArtifactFingerprint(
        artifact.strategy_id,
        artifact.strategy_version,
        artifact.algorithm_sha256,
        artifact.config_sha256,
        artifact.feature_pipeline_sha256,
        artifact.fitted_state_sha256,
        artifact.seed,
        artifact.fit_cutoff_at,
        artifact.created_at,
    )


def _validate_candidate_score(score: CandidateScore) -> CandidateScore:
    if not isinstance(score, CandidateScore):
        raise TradingResearchError("candidate score must be CandidateScore evidence")
    artifact = _validated_strategy_artifact(score.strategy_artifact)
    if artifact.fingerprint != score._artifact_fingerprint:
        raise TradingResearchError("candidate strategy artifact changed after construction")
    _require_identity(score.metric_name, "metric_name")
    _require_digest(score.metric_fingerprint, "metric_fingerprint")
    _require_metric(score.metric_value, "candidate metric", allow_none=True)
    _require_digest(score.dataset_semantic_hash, "dataset_semantic_hash")
    _require_data_quality(score.data_quality)
    _require_digest(score.universe_fingerprint, "universe_fingerprint")
    _require_aware_utc(score.universe_cutoff_at, "universe_cutoff_at")
    _require_aware_utc(score.evaluated_at, "evaluated_at")
    if _candidate_fingerprint(score) != score._evidence_fingerprint:
        raise TradingResearchError("candidate score evidence changed after construction")
    return score


def _validate_selection_identity(
    selection: SelectionDecision,
) -> tuple[datetime, datetime]:
    if not isinstance(selection, SelectionDecision):
        raise TradingResearchError("selection must be SelectionDecision evidence")
    artifact = _validated_strategy_artifact(selection.strategy_artifact)
    if artifact.fingerprint != selection.strategy_artifact_fingerprint:
        raise TradingResearchError("selected strategy artifact identity changed")
    _require_identity(selection.metric_name, "metric_name")
    _require_digest(selection.metric_fingerprint, "metric_fingerprint")
    _require_metric(selection.metric_value, "selection metric")
    _require_digest(selection.dataset_semantic_hash, "dataset_semantic_hash")
    _require_digest(selection.universe_fingerprint, "universe_fingerprint")
    _require_digest(selection.protocol_fingerprint, "protocol_fingerprint")
    _require_data_quality(selection.data_quality)
    if selection.source_partition is not Partition.VALIDATION:
        raise CausalityViolation("strategy selection must use validation results only")
    if not isinstance(selection.higher_is_better, bool):
        raise TradingResearchError("higher_is_better must be a boolean")
    if not selection.data_quality.is_clean:
        raise TradingResearchError("selection cannot carry dirty dataset quality evidence")
    selected_at = _require_aware_utc(selection.selected_at, "selected_at")
    universe_cutoff_at = _require_aware_utc(
        selection.universe_cutoff_at,
        "universe_cutoff_at",
    )
    if _selection_fingerprint(selection) != selection._evidence_fingerprint:
        raise TradingResearchError("selection evidence changed after construction")
    return selected_at, universe_cutoff_at


def _validate_partition_result_identity(result: PartitionResult) -> None:
    if not isinstance(result, PartitionResult):
        raise TradingResearchError("result must be PartitionResult evidence")
    artifact = _validated_strategy_artifact(result.strategy_artifact)
    if artifact.fingerprint != result._artifact_fingerprint:
        raise TradingResearchError("result strategy artifact changed after construction")
    _require_identity(result.metric_name, "metric_name")
    _require_digest(result.metric_fingerprint, "metric_fingerprint")
    _require_metric(result.metric_value, "partition metric", allow_none=True)
    _require_digest(result.dataset_semantic_hash, "dataset_semantic_hash")
    _require_digest(result.universe_fingerprint, "universe_fingerprint")
    _require_data_quality(result.data_quality)
    _require_aware_utc(result.universe_cutoff_at, "universe_cutoff_at")
    _require_aware_utc(result.evaluated_at, "evaluated_at")
    if _result_fingerprint(result) != result._evidence_fingerprint:
        raise TradingResearchError("partition result evidence changed after construction")


def _copy_selection(selection: SelectionDecision) -> SelectionDecision:
    _validate_selection_identity(selection)
    return SelectionDecision._create(
        strategy_artifact=selection.strategy_artifact,
        metric_name=selection.metric_name,
        metric_fingerprint=selection.metric_fingerprint,
        metric_value=selection.metric_value,
        dataset_semantic_hash=selection.dataset_semantic_hash,
        data_quality=selection.data_quality,
        universe_fingerprint=selection.universe_fingerprint,
        universe_cutoff_at=selection.universe_cutoff_at,
        selected_at=selection.selected_at,
        protocol_fingerprint=selection.protocol_fingerprint,
        higher_is_better=selection.higher_is_better,
    )


def _copy_result(result: PartitionResult) -> PartitionResult:
    _validate_partition_result_identity(result)
    return PartitionResult(
        strategy_artifact=result.strategy_artifact,
        partition=result.partition,
        metric_name=result.metric_name,
        metric_fingerprint=result.metric_fingerprint,
        metric_value=result.metric_value,
        dataset_semantic_hash=result.dataset_semantic_hash,
        data_quality=_copy_quality(result.data_quality),
        universe_fingerprint=result.universe_fingerprint,
        universe_cutoff_at=result.universe_cutoff_at,
        evaluated_at=result.evaluated_at,
    )


def _copy_quality(value: ReplayDataQuality) -> ReplayDataQuality:
    _require_data_quality(value)
    return ReplayDataQuality(
        value.duplicate_count,
        value.conflict_count,
        value.gap_count,
        value.evidence_sha256,
    )


def _protocol_fingerprint(protocol: HeldOutProtocol) -> str:
    payload = (
        f"{_HELDOUT_SCHEMA}|{protocol.refit_policy.value}|{protocol.train.partition.value}|"
        f"{protocol.train.start_at.isoformat()}|{protocol.train.end_at.isoformat()}|"
        f"{protocol.validation.partition.value}|{protocol.validation.start_at.isoformat()}|"
        f"{protocol.validation.end_at.isoformat()}|{protocol.test.partition.value}|"
        f"{protocol.test.start_at.isoformat()}|{protocol.test.end_at.isoformat()}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _candidate_fingerprint(score: CandidateScore) -> str:
    metric_value = "none" if score.metric_value is None else str(score.metric_value)
    payload = "|".join(
        (
            _CANDIDATE_SCHEMA,
            score._artifact_fingerprint,
            score.partition.value,
            score.metric_name,
            score.metric_fingerprint,
            metric_value,
            score.dataset_semantic_hash,
            _quality_fingerprint(score.data_quality),
            score.universe_fingerprint,
            score.universe_cutoff_at.isoformat(),
            score.evaluated_at.isoformat(),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _assessment_fingerprint(
    protocol: HeldOutProtocol,
    selection: SelectionDecision,
    result: PartitionResult,
) -> str:
    payload = "|".join(
        (
            _ASSESSMENT_SCHEMA,
            protocol.fingerprint,
            _selection_fingerprint(selection),
            _result_fingerprint(result),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _selection_fingerprint(selection: SelectionDecision) -> str:
    value = selection.metric_value
    payload = "|".join(
        (
            _SELECTION_SCHEMA,
            selection.strategy_artifact_fingerprint,
            selection.metric_name,
            selection.metric_fingerprint,
            str(value),
            selection.dataset_semantic_hash,
            _quality_fingerprint(selection.data_quality),
            selection.universe_fingerprint,
            selection.universe_cutoff_at.isoformat(),
            selection.selected_at.isoformat(),
            selection.protocol_fingerprint,
            str(selection.higher_is_better),
            selection.source_partition.value,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _result_fingerprint(result: PartitionResult) -> str:
    metric_value = "none" if result.metric_value is None else str(result.metric_value)
    payload = "|".join(
        (
            _RESULT_SCHEMA,
            result._artifact_fingerprint,
            result.partition.value,
            result.metric_name,
            result.metric_fingerprint,
            metric_value,
            result.dataset_semantic_hash,
            _quality_fingerprint(result.data_quality),
            result.universe_fingerprint,
            result.universe_cutoff_at.isoformat(),
            result.evaluated_at.isoformat(),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _quality_fingerprint(value: ReplayDataQuality) -> str:
    return "|".join(
        (
            str(value.duplicate_count),
            str(value.conflict_count),
            str(value.gap_count),
            value.evidence_sha256,
        )
    )


def _require_aware_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TradingResearchError(f"{field_name} must be a datetime")
    return require_aware_utc(value, field_name)


def _require_data_quality(value: ReplayDataQuality) -> None:
    if not isinstance(value, ReplayDataQuality):
        raise TradingResearchError("data_quality must be ReplayDataQuality evidence")
    ReplayDataQuality(
        value.duplicate_count,
        value.conflict_count,
        value.gap_count,
        value.evidence_sha256,
    )


def _require_identity(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise TradingResearchError(f"{field_name} must be a canonical non-empty identity")


def _require_metric(
    value: Decimal | None,
    field_name: str,
    *,
    allow_none: bool = False,
) -> None:
    if value is None:
        if allow_none:
            return
        raise TradingResearchError(f"{field_name} must be present")
    if not isinstance(value, Decimal) or not value.is_finite():
        raise TradingResearchError(f"{field_name} must be a finite Decimal")


def _require_digest(value: str, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise TradingResearchError(
            f"{field_name} must be a canonical lowercase SHA-256 digest"
        )
