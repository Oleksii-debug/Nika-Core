from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from nika_core.trading_research.metric_evidence import (
    MetricEvidence,
    calculate_metric_evidence,
)
from nika_core.trading_research.metrics import EquityPoint, SamplingMode, SamplingSpec


def synthetic_daily_sampling() -> SamplingSpec:
    return SamplingSpec(
        SamplingMode.REGULAR,
        "synthetic-one-period-year-v1",
        timedelta(days=1),
        Decimal(1),
    )


def total_return_evidence(
    start: datetime,
    value: str | Decimal,
    *,
    middle: str | Decimal | None = None,
) -> MetricEvidence:
    metric_value = value if isinstance(value, Decimal) else Decimal(value)
    start_equity = Decimal(100)
    end_equity = start_equity * (Decimal(1) + metric_value)
    points = [EquityPoint(start, start_equity)]
    if middle is not None:
        middle_equity = middle if isinstance(middle, Decimal) else Decimal(middle)
        points.append(EquityPoint(start + timedelta(days=1), middle_equity))
        end_at = start + timedelta(days=2)
    else:
        end_at = start + timedelta(days=1)
    points.append(EquityPoint(end_at, end_equity))
    return calculate_metric_evidence(
        tuple(points),
        trade_count=max(1, len(points) - 1),
        sampling=synthetic_daily_sampling(),
        metric_name="total_return",
    )


def sharpe_two_evidence(start: datetime) -> MetricEvidence:
    return calculate_metric_evidence(
        (
            EquityPoint(start, Decimal(100)),
            EquityPoint(start + timedelta(days=1), Decimal(101)),
            EquityPoint(start + timedelta(days=2), Decimal("103.02")),
            EquityPoint(start + timedelta(days=3), Decimal("106.1106")),
        ),
        trade_count=3,
        sampling=synthetic_daily_sampling(),
        metric_name="sharpe_ratio",
    )


def unavailable_sharpe_evidence(start: datetime) -> MetricEvidence:
    return calculate_metric_evidence(
        (
            EquityPoint(start, Decimal(100)),
            EquityPoint(start + timedelta(days=1), Decimal(100)),
        ),
        trade_count=0,
        sampling=synthetic_daily_sampling(),
        metric_name="sharpe_ratio",
    )
