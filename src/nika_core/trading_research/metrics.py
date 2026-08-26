from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from enum import StrEnum
from itertools import pairwise

from .contracts import TradingResearchError, require_aware_utc

_METRIC_CONTEXT = Context(prec=34, rounding=ROUND_HALF_EVEN)
_SAMPLING_SCHEMA = "nika-trader-sampling-v1"


class RatioUnavailableReason(StrEnum):
    NO_RETURNS = "no_returns"
    NO_TRADES = "no_trades"
    INSUFFICIENT_RETURNS = "insufficient_returns"
    ZERO_VOLATILITY = "zero_volatility"
    NO_DOWNSIDE = "no_downside"
    ANNUALIZATION_UNAVAILABLE = "annualization_unavailable"


class SamplingMode(StrEnum):
    REGULAR = "regular"
    IRREGULAR = "irregular"


class MissingPeriodPolicy(StrEnum):
    REJECT = "reject"


@dataclass(frozen=True, slots=True)
class SamplingSpec:
    mode: SamplingMode
    calendar_id: str | None
    cadence: timedelta | None
    periods_per_year: Decimal | None
    missing_period_policy: MissingPeriodPolicy = MissingPeriodPolicy.REJECT
    _sealed_fingerprint: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.mode, SamplingMode):
            raise TradingResearchError("sampling mode must be a SamplingMode")
        if not isinstance(self.missing_period_policy, MissingPeriodPolicy):
            raise TradingResearchError("missing-period policy must be a MissingPeriodPolicy")
        if self.missing_period_policy is not MissingPeriodPolicy.REJECT:
            raise TradingResearchError("unsupported missing-period policy")
        if self.mode is SamplingMode.REGULAR:
            if (
                not isinstance(self.calendar_id, str)
                or not self.calendar_id
                or self.calendar_id != self.calendar_id.strip()
            ):
                raise TradingResearchError(
                    "regular sampling requires a canonical calendar identity"
                )
            if not isinstance(self.cadence, timedelta) or self.cadence <= timedelta(0):
                raise TradingResearchError(
                    "regular sampling cadence must be a positive timedelta"
                )
            value = self.periods_per_year
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise TradingResearchError(
                    "regular sampling periods_per_year must be a finite positive Decimal"
                )
            with localcontext(_METRIC_CONTEXT):
                object.__setattr__(self, "periods_per_year", +value)
        else:
            if self.calendar_id is not None:
                raise TradingResearchError(
                    "irregular sampling cannot claim a calendar identity"
                )
            if self.cadence is not None or self.periods_per_year is not None:
                raise TradingResearchError(
                    "irregular sampling cannot claim cadence or annualization periods"
                )
        object.__setattr__(self, "_sealed_fingerprint", _sampling_fingerprint(self))

    @property
    def fingerprint(self) -> str:
        return self._sealed_fingerprint


@dataclass(frozen=True, slots=True)
class EquityPoint:
    at: datetime
    equity: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.equity, Decimal):
            raise TradingResearchError("equity must be a Decimal")
        at = _require_aware_utc(self.at, "at")
        if not self.equity.is_finite() or self.equity < 0:
            raise TradingResearchError("equity must be finite and non-negative")
        object.__setattr__(self, "at", at)


@dataclass(frozen=True, slots=True)
class PerformanceMetrics:
    observation_count: int
    return_count: int
    trade_count: int
    sampling_fingerprint: str
    sampling_mode: SamplingMode
    calendar_id: str | None
    cadence_seconds: Decimal | None
    periods_per_year: Decimal | None
    risk_free_rate_per_period: Decimal
    minimum_acceptable_return_per_period: Decimal
    total_return: Decimal
    max_drawdown: Decimal
    sharpe_ratio: Decimal | None
    sortino_ratio: Decimal | None
    sharpe_unavailable_reason: RatioUnavailableReason | None
    sortino_unavailable_reason: RatioUnavailableReason | None

    @property
    def no_trade(self) -> bool:
        return self.trade_count == 0


def _require_aware_utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TradingResearchError(f"{field_name} must be a datetime")
    return require_aware_utc(value, field_name)


def _timedelta_microseconds(value: timedelta) -> int:
    return (
        value.days * 86_400_000_000
        + value.seconds * 1_000_000
        + value.microseconds
    )


def _sampling_fingerprint(sampling: SamplingSpec) -> str:
    cadence_microseconds = (
        "none"
        if sampling.cadence is None
        else str(_timedelta_microseconds(sampling.cadence))
    )
    periods = (
        "none" if sampling.periods_per_year is None else str(sampling.periods_per_year)
    )
    payload = "|".join(
        (
            _SAMPLING_SCHEMA,
            sampling.mode.value,
            sampling.calendar_id or "none",
            cadence_microseconds,
            periods,
            sampling.missing_period_policy.value,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_points(points: Sequence[EquityPoint]) -> tuple[EquityPoint, ...]:
    materialized = tuple(points)
    if not materialized:
        raise TradingResearchError("at least one equity observation is required")
    if any(not isinstance(point, EquityPoint) for point in materialized):
        raise TradingResearchError("equity observations must be EquityPoint values")
    if materialized[0].equity <= 0:
        raise TradingResearchError("initial equity must be positive")
    previous_at: datetime | None = None
    previous_equity: Decimal | None = None
    for point in materialized:
        if previous_at is not None and point.at <= previous_at:
            raise TradingResearchError("equity timestamps must be strictly increasing")
        if previous_equity == 0:
            raise TradingResearchError("equity cannot recover after reaching zero")
        previous_at = point.at
        previous_equity = point.equity
    return materialized


def _validate_sampling(
    points: tuple[EquityPoint, ...],
    sampling: SamplingSpec,
) -> None:
    if not isinstance(sampling, SamplingSpec):
        raise TradingResearchError("sampling must be SamplingSpec evidence")
    validated = SamplingSpec(
        sampling.mode,
        sampling.calendar_id,
        sampling.cadence,
        sampling.periods_per_year,
        sampling.missing_period_policy,
    )
    if validated.fingerprint != sampling.fingerprint:
        raise TradingResearchError("sampling evidence changed after construction")
    if validated.mode is SamplingMode.IRREGULAR:
        return
    assert validated.cadence is not None
    for previous, current in pairwise(points):
        if current.at - previous.at != validated.cadence:
            raise TradingResearchError(
                "regular sampling observations do not match the declared cadence"
            )


def returns_from_equity(points: Sequence[EquityPoint]) -> tuple[Decimal, ...]:
    materialized = _validate_points(points)
    with localcontext(_METRIC_CONTEXT):
        return tuple(
            (current.equity / previous.equity) - Decimal(1)
            for previous, current in pairwise(materialized)
        )


def max_drawdown(points: Sequence[EquityPoint]) -> Decimal:
    materialized = _validate_points(points)
    with localcontext(_METRIC_CONTEXT):
        peak = materialized[0].equity
        maximum = Decimal(0)
        for point in materialized[1:]:
            if point.equity > peak:
                peak = point.equity
                continue
            drawdown = (peak - point.equity) / peak
            maximum = max(maximum, drawdown)
        return +maximum


def _validate_rate(value: Decimal, field_name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise TradingResearchError(f"{field_name} must be a finite Decimal")
    return value


def _mean(values: tuple[Decimal, ...]) -> Decimal:
    return sum(values, Decimal(0)) / Decimal(len(values))


def _sample_standard_deviation(values: tuple[Decimal, ...]) -> Decimal:
    mean = _mean(values)
    variance = sum((value - mean) ** 2 for value in values) / Decimal(len(values) - 1)
    return variance.sqrt()


def _ratio_reasons_for_empty_or_short(
    returns: tuple[Decimal, ...],
    trade_count: int,
) -> tuple[RatioUnavailableReason | None, RatioUnavailableReason | None]:
    if trade_count == 0:
        return RatioUnavailableReason.NO_TRADES, RatioUnavailableReason.NO_TRADES
    if not returns:
        return RatioUnavailableReason.NO_RETURNS, RatioUnavailableReason.NO_RETURNS
    if len(returns) < 2:
        reason = RatioUnavailableReason.INSUFFICIENT_RETURNS
        return reason, reason
    return None, None


def _cadence_seconds(sampling: SamplingSpec) -> Decimal | None:
    if sampling.cadence is None:
        return None
    microseconds = _timedelta_microseconds(sampling.cadence)
    with localcontext(_METRIC_CONTEXT):
        return Decimal(microseconds) / Decimal(1_000_000)


def calculate_performance(
    points: Sequence[EquityPoint],
    *,
    trade_count: int,
    sampling: SamplingSpec,
    risk_free_rate_per_period: Decimal = Decimal(0),
    minimum_acceptable_return_per_period: Decimal = Decimal(0),
) -> PerformanceMetrics:
    if isinstance(trade_count, bool) or not isinstance(trade_count, int) or trade_count < 0:
        raise TradingResearchError("trade_count must be a non-negative integer")
    risk_free = _validate_rate(risk_free_rate_per_period, "risk_free_rate_per_period")
    minimum_acceptable = _validate_rate(
        minimum_acceptable_return_per_period,
        "minimum_acceptable_return_per_period",
    )
    materialized = _validate_points(points)
    _validate_sampling(materialized, sampling)

    with localcontext(_METRIC_CONTEXT):
        returns = returns_from_equity(materialized)
        total_return = (materialized[-1].equity / materialized[0].equity) - Decimal(1)
        drawdown = max_drawdown(materialized)
        sharpe_reason, sortino_reason = _ratio_reasons_for_empty_or_short(
            returns,
            trade_count,
        )
        sharpe: Decimal | None = None
        sortino: Decimal | None = None

        annualization: Decimal | None = None
        if sampling.mode is SamplingMode.REGULAR:
            assert sampling.periods_per_year is not None
            annualization = sampling.periods_per_year.sqrt()
        elif sharpe_reason is None or sortino_reason is None:
            unavailable = RatioUnavailableReason.ANNUALIZATION_UNAVAILABLE
            if sharpe_reason is None:
                sharpe_reason = unavailable
            if sortino_reason is None:
                sortino_reason = unavailable

        if sharpe_reason is None:
            assert annualization is not None
            excess = tuple(value - risk_free for value in returns)
            volatility = _sample_standard_deviation(excess)
            if volatility == 0:
                sharpe_reason = RatioUnavailableReason.ZERO_VOLATILITY
            else:
                sharpe = (_mean(excess) / volatility) * annualization

        if sortino_reason is None:
            assert annualization is not None
            excess_over_minimum = tuple(value - minimum_acceptable for value in returns)
            downside_squares = tuple(
                min(value, Decimal(0)) ** 2 for value in excess_over_minimum
            )
            downside_deviation = (
                sum(downside_squares, Decimal(0)) / Decimal(len(downside_squares))
            ).sqrt()
            if downside_deviation == 0:
                sortino_reason = RatioUnavailableReason.NO_DOWNSIDE
            else:
                sortino = (_mean(excess_over_minimum) / downside_deviation) * annualization

        return PerformanceMetrics(
            observation_count=len(materialized),
            return_count=len(returns),
            trade_count=trade_count,
            sampling_fingerprint=sampling.fingerprint,
            sampling_mode=sampling.mode,
            calendar_id=sampling.calendar_id,
            cadence_seconds=_cadence_seconds(sampling),
            periods_per_year=sampling.periods_per_year,
            risk_free_rate_per_period=+risk_free,
            minimum_acceptable_return_per_period=+minimum_acceptable,
            total_return=+total_return,
            max_drawdown=+drawdown,
            sharpe_ratio=None if sharpe is None else +sharpe,
            sortino_ratio=None if sortino is None else +sortino,
            sharpe_unavailable_reason=sharpe_reason,
            sortino_unavailable_reason=sortino_reason,
        )
