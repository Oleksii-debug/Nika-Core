from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext
from enum import StrEnum
from itertools import pairwise

from .contracts import TradingResearchError, require_aware_utc

_METRIC_CONTEXT = Context(prec=34, rounding=ROUND_HALF_EVEN)


class RatioUnavailableReason(StrEnum):
    NO_RETURNS = "no_returns"
    NO_TRADES = "no_trades"
    INSUFFICIENT_RETURNS = "insufficient_returns"
    ZERO_VOLATILITY = "zero_volatility"
    NO_DOWNSIDE = "no_downside"


@dataclass(frozen=True, slots=True)
class EquityPoint:
    at: datetime
    equity: Decimal

    def __post_init__(self) -> None:
        at = require_aware_utc(self.at, "at")
        if not self.equity.is_finite() or self.equity < 0:
            raise TradingResearchError("equity must be finite and non-negative")
        object.__setattr__(self, "at", at)


@dataclass(frozen=True, slots=True)
class PerformanceMetrics:
    observation_count: int
    return_count: int
    trade_count: int
    periods_per_year: Decimal
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


def _validate_points(points: Sequence[EquityPoint]) -> tuple[EquityPoint, ...]:
    materialized = tuple(points)
    if not materialized:
        raise TradingResearchError("at least one equity observation is required")
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
    if not value.is_finite():
        raise TradingResearchError(f"{field_name} must be finite")
    return value


def _mean(values: tuple[Decimal, ...]) -> Decimal:
    return sum(values, Decimal(0)) / Decimal(len(values))


def _sample_standard_deviation(values: tuple[Decimal, ...]) -> Decimal:
    mean = _mean(values)
    variance = sum((value - mean) ** 2 for value in values) / Decimal(len(values) - 1)
    return variance.sqrt()


def _ratio_reasons_for_empty_or_short(
    returns: tuple[Decimal, ...], trade_count: int
) -> tuple[RatioUnavailableReason | None, RatioUnavailableReason | None]:
    if trade_count == 0:
        return RatioUnavailableReason.NO_TRADES, RatioUnavailableReason.NO_TRADES
    if not returns:
        return RatioUnavailableReason.NO_RETURNS, RatioUnavailableReason.NO_RETURNS
    if len(returns) < 2:
        reason = RatioUnavailableReason.INSUFFICIENT_RETURNS
        return reason, reason
    return None, None


def calculate_performance(
    points: Sequence[EquityPoint],
    *,
    trade_count: int,
    periods_per_year: Decimal,
    risk_free_rate_per_period: Decimal = Decimal(0),
    minimum_acceptable_return_per_period: Decimal = Decimal(0),
) -> PerformanceMetrics:
    if isinstance(trade_count, bool) or not isinstance(trade_count, int) or trade_count < 0:
        raise TradingResearchError("trade_count must be a non-negative integer")
    if not periods_per_year.is_finite() or periods_per_year <= 0:
        raise TradingResearchError("periods_per_year must be finite and positive")
    risk_free = _validate_rate(risk_free_rate_per_period, "risk_free_rate_per_period")
    minimum_acceptable = _validate_rate(
        minimum_acceptable_return_per_period,
        "minimum_acceptable_return_per_period",
    )
    materialized = _validate_points(points)

    with localcontext(_METRIC_CONTEXT):
        returns = returns_from_equity(materialized)
        total_return = (materialized[-1].equity / materialized[0].equity) - Decimal(1)
        drawdown = max_drawdown(materialized)
        sharpe_reason, sortino_reason = _ratio_reasons_for_empty_or_short(returns, trade_count)
        sharpe: Decimal | None = None
        sortino: Decimal | None = None
        annualization = periods_per_year.sqrt()

        if sharpe_reason is None:
            excess = tuple(value - risk_free for value in returns)
            volatility = _sample_standard_deviation(excess)
            if volatility == 0:
                sharpe_reason = RatioUnavailableReason.ZERO_VOLATILITY
            else:
                sharpe = (_mean(excess) / volatility) * annualization

        if sortino_reason is None:
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
            periods_per_year=+periods_per_year,
            risk_free_rate_per_period=+risk_free,
            minimum_acceptable_return_per_period=+minimum_acceptable,
            total_return=+total_return,
            max_drawdown=+drawdown,
            sharpe_ratio=None if sharpe is None else +sharpe,
            sortino_ratio=None if sortino is None else +sortino,
            sharpe_unavailable_reason=sharpe_reason,
            sortino_unavailable_reason=sortino_reason,
        )
