from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from .contracts import TradingResearchError
from .metrics import (
    EquityPoint,
    RatioUnavailableReason,
    SamplingSpec,
    calculate_performance,
    returns_from_equity,
)

_DEFINITION_SCHEMA = "nika-trader-metric-definition-v1"
_EVIDENCE_SCHEMA = "nika-trader-metric-evidence-v1"
_DEFINITIONS = {
    "total_return": "terminal_over_initial_minus_one_decimal34_half_even_v1",
    "max_drawdown": "max_peak_to_trough_fraction_decimal34_half_even_v1",
    "sharpe_ratio": "mean_excess_over_sample_stddev_times_sqrt_annualization_decimal34_v1",
    "sortino_ratio": "mean_excess_over_downside_deviation_times_sqrt_annualization_decimal34_v1",
}


@dataclass(frozen=True, slots=True, init=False)
class MetricEvidence:
    metric_name: str
    definition_sha256: str
    sampling_fingerprint: str
    equity_trace_sha256: str
    return_trace_sha256: str
    trade_count: int
    risk_free_rate_per_period: Decimal
    minimum_acceptable_return_per_period: Decimal
    value: Decimal | None
    unavailable_reason: RatioUnavailableReason | None
    evidence_sha256: str

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("MetricEvidence instances are created only by calculate_metric_evidence()")

    @classmethod
    def _create(
        cls,
        *,
        metric_name: str,
        sampling_fingerprint: str,
        equity_trace_sha256: str,
        return_trace_sha256: str,
        trade_count: int,
        risk_free_rate_per_period: Decimal,
        minimum_acceptable_return_per_period: Decimal,
        value: Decimal | None,
        unavailable_reason: RatioUnavailableReason | None,
    ) -> MetricEvidence:
        obj = object.__new__(cls)
        values = {
            "metric_name": metric_name,
            "definition_sha256": _definition_sha256(metric_name),
            "sampling_fingerprint": sampling_fingerprint,
            "equity_trace_sha256": equity_trace_sha256,
            "return_trace_sha256": return_trace_sha256,
            "trade_count": trade_count,
            "risk_free_rate_per_period": risk_free_rate_per_period,
            "minimum_acceptable_return_per_period": minimum_acceptable_return_per_period,
            "value": value,
            "unavailable_reason": unavailable_reason,
        }
        _validate_fields(**values)
        for name, field_value in values.items():
            object.__setattr__(obj, name, field_value)
        object.__setattr__(obj, "evidence_sha256", _fingerprint(obj))
        return obj


def calculate_metric_evidence(
    points: Sequence[EquityPoint],
    *,
    trade_count: int,
    sampling: SamplingSpec,
    metric_name: str,
    risk_free_rate_per_period: Decimal = Decimal(0),
    minimum_acceptable_return_per_period: Decimal = Decimal(0),
) -> MetricEvidence:
    materialized = tuple(points)
    metrics = calculate_performance(
        materialized,
        trade_count=trade_count,
        sampling=sampling,
        risk_free_rate_per_period=risk_free_rate_per_period,
        minimum_acceptable_return_per_period=minimum_acceptable_return_per_period,
    )
    returns = returns_from_equity(materialized)
    if metric_name == "total_return":
        value, reason = metrics.total_return, None
    elif metric_name == "max_drawdown":
        value, reason = metrics.max_drawdown, None
    elif metric_name == "sharpe_ratio":
        value, reason = metrics.sharpe_ratio, metrics.sharpe_unavailable_reason
    elif metric_name == "sortino_ratio":
        value, reason = metrics.sortino_ratio, metrics.sortino_unavailable_reason
    else:
        raise TradingResearchError("metric_name must identify a canonical DEV26 metric")
    equity_trace = tuple((point.at.isoformat(), str(point.equity)) for point in materialized)
    return_trace = tuple(str(value) for value in returns)
    return MetricEvidence._create(
        metric_name=metric_name,
        sampling_fingerprint=sampling.fingerprint,
        equity_trace_sha256=_trace_sha256("equity", equity_trace),
        return_trace_sha256=_trace_sha256("returns", return_trace),
        trade_count=trade_count,
        risk_free_rate_per_period=metrics.risk_free_rate_per_period,
        minimum_acceptable_return_per_period=metrics.minimum_acceptable_return_per_period,
        value=value,
        unavailable_reason=reason,
    )


def validate_metric_evidence(evidence: MetricEvidence) -> MetricEvidence:
    if not isinstance(evidence, MetricEvidence):
        raise TradingResearchError("metric_evidence must be MetricEvidence evidence")
    values = {
        "metric_name": evidence.metric_name,
        "definition_sha256": evidence.definition_sha256,
        "sampling_fingerprint": evidence.sampling_fingerprint,
        "equity_trace_sha256": evidence.equity_trace_sha256,
        "return_trace_sha256": evidence.return_trace_sha256,
        "trade_count": evidence.trade_count,
        "risk_free_rate_per_period": evidence.risk_free_rate_per_period,
        "minimum_acceptable_return_per_period": evidence.minimum_acceptable_return_per_period,
        "value": evidence.value,
        "unavailable_reason": evidence.unavailable_reason,
    }
    _validate_fields(**values)
    if evidence.evidence_sha256 != _fingerprint(evidence):
        raise TradingResearchError("metric evidence changed after construction")
    return MetricEvidence._create(
        metric_name=evidence.metric_name,
        sampling_fingerprint=evidence.sampling_fingerprint,
        equity_trace_sha256=evidence.equity_trace_sha256,
        return_trace_sha256=evidence.return_trace_sha256,
        trade_count=evidence.trade_count,
        risk_free_rate_per_period=evidence.risk_free_rate_per_period,
        minimum_acceptable_return_per_period=evidence.minimum_acceptable_return_per_period,
        value=evidence.value,
        unavailable_reason=evidence.unavailable_reason,
    )


def _definition_sha256(metric_name: str) -> str:
    definition = _DEFINITIONS.get(metric_name)
    if definition is None:
        raise TradingResearchError("metric_name must identify a canonical DEV26 metric")
    payload = f"{_DEFINITION_SCHEMA}|{metric_name}|{definition}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_fields(
    *,
    metric_name: str,
    definition_sha256: str,
    sampling_fingerprint: str,
    equity_trace_sha256: str,
    return_trace_sha256: str,
    trade_count: int,
    risk_free_rate_per_period: Decimal,
    minimum_acceptable_return_per_period: Decimal,
    value: Decimal | None,
    unavailable_reason: RatioUnavailableReason | None,
) -> None:
    if definition_sha256 != _definition_sha256(metric_name):
        raise TradingResearchError("metric evidence definition changed after construction")
    for digest, field_name in (
        (sampling_fingerprint, "sampling_fingerprint"),
        (equity_trace_sha256, "equity_trace_sha256"),
        (return_trace_sha256, "return_trace_sha256"),
    ):
        _require_digest(digest, field_name)
    if isinstance(trade_count, bool) or not isinstance(trade_count, int) or trade_count < 0:
        raise TradingResearchError("metric evidence trade_count must be a non-negative integer")
    for rate, field_name in (
        (risk_free_rate_per_period, "risk_free_rate_per_period"),
        (minimum_acceptable_return_per_period, "minimum_acceptable_return_per_period"),
    ):
        if not isinstance(rate, Decimal) or not rate.is_finite():
            raise TradingResearchError(f"{field_name} must be a finite Decimal")
    if value is not None and (not isinstance(value, Decimal) or not value.is_finite()):
        raise TradingResearchError("metric evidence value must be a finite Decimal or None")
    if unavailable_reason is not None and not isinstance(unavailable_reason, RatioUnavailableReason):
        raise TradingResearchError("metric unavailable reason must be typed")
    if metric_name in {"total_return", "max_drawdown"}:
        if value is None or unavailable_reason is not None:
            raise TradingResearchError("deterministic metric evidence must contain its value")
    elif (value is None) == (unavailable_reason is None):
        raise TradingResearchError(
            "ratio metric evidence must contain exactly one of value or unavailable reason"
        )


def _trace_sha256(kind: str, values: tuple[object, ...]) -> str:
    payload = json.dumps(
        (_EVIDENCE_SCHEMA, kind, values),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _fingerprint(evidence: MetricEvidence) -> str:
    value = "none" if evidence.value is None else str(evidence.value)
    reason = "none" if evidence.unavailable_reason is None else evidence.unavailable_reason.value
    payload = "|".join(
        (
            _EVIDENCE_SCHEMA,
            evidence.metric_name,
            evidence.definition_sha256,
            evidence.sampling_fingerprint,
            evidence.equity_trace_sha256,
            evidence.return_trace_sha256,
            str(evidence.trade_count),
            str(evidence.risk_free_rate_per_period),
            str(evidence.minimum_acceptable_return_per_period),
            value,
            reason,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _require_digest(value: str, field_name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise TradingResearchError(
            f"{field_name} must be a canonical lowercase SHA-256 digest"
        )
