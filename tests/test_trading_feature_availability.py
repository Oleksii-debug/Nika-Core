from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from nika_core.trading_research import (
    CausalityViolation,
    FeatureLineage,
    FeaturePoint,
    causal_shift,
    fill_missing,
    trailing_mean,
)

BASE = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize("transform", ["shift", "mean", "fill", "identity"])
def test_feature_transforms_reject_future_available_input(
    transform: str,
) -> None:
    # First value would not exist at the decision timestamp of the second point.
    points = (
        FeaturePoint(Decimal(7), BASE + timedelta(minutes=5)),
        FeaturePoint(None, BASE + timedelta(minutes=1)),
        FeaturePoint(Decimal(9), BASE + timedelta(minutes=6)),
    )
    with pytest.raises(CausalityViolation, match="ordered by availability"):
        if transform == "shift":
            causal_shift(points, 1)
        elif transform == "mean":
            trailing_mean(points, 2)
        elif transform == "fill":
            fill_missing(points)
        else:
            causal_shift(points, 0)


def test_equal_availability_time_and_causal_sequence_remain_valid() -> None:
    points = (
        FeaturePoint(Decimal(2), BASE),
        FeaturePoint(None, BASE),
        FeaturePoint(Decimal(4), BASE + timedelta(minutes=1)),
    )
    assert tuple(point.value for point in causal_shift(points, 1)) == (
        None,
        Decimal(2),
        None,
    )
    assert tuple(point.value for point in trailing_mean(points, 2)) == (
        Decimal(2),
        Decimal(2),
        Decimal(4),
    )
    assert tuple(point.value for point in fill_missing(points)) == (
        Decimal(2),
        Decimal(2),
        Decimal(4),
    )
    assert tuple(point.available_at for point in fill_missing(points)) == tuple(
        point.available_at for point in points
    )


@pytest.mark.parametrize(
    ("input_names", "input_times"),
    [
        (("known", "future"), (BASE,)),
        (("known",), (BASE, BASE)),
    ],
)
def test_feature_lineage_cannot_omit_or_invent_input_availability(
    input_names: tuple[str, ...], input_times: tuple[datetime, ...]
) -> None:
    with pytest.raises(CausalityViolation, match="every input"):
        FeatureLineage("derived", input_names, input_times, BASE)


@pytest.mark.parametrize(
    ("name", "input_names"),
    [
        (" ", ("known",)),
        ("derived", (" ",)),
        ("derived", (123,)),
    ],
)
def test_feature_lineage_requires_meaningful_text_identity(
    name: str, input_names: tuple[str, ...]
) -> None:
    with pytest.raises(CausalityViolation, match="name"):
        FeatureLineage(name, input_names, (BASE,), BASE)


def test_complete_feature_lineage_accepts_latest_input_timestamp() -> None:
    later = BASE + timedelta(minutes=2)
    lineage = FeatureLineage(
        "spread",
        ("bid", "ask"),
        (BASE, later),
        later,
    )
    assert lineage.available_at == later
    assert lineage.input_available_at == (BASE, later)
