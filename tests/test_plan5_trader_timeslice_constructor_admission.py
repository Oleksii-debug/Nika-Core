"""Adversarial constructor-ingress checks for the canonical paper replay TimeSlice."""
from __future__ import annotations

from datetime import UTC, datetime, tzinfo, timedelta
from decimal import Decimal

import pytest

from nika_core.trading_research.contracts import (
    EventTime, Instrument, Quote, TradingResearchError, Venue,
)
from nika_core.trading_research.replay import TimeSlice

NOW = datetime(2026, 1, 1, tzinfo=UTC)
INSTRUMENT = Instrument("TEST", Venue("SIM", "UTC"), "USD")


def _quote() -> Quote:
    return Quote(
        INSTRUMENT, EventTime(NOW, NOW, NOW),
        Decimal("99"), Decimal("101"), Decimal("2"), Decimal("3"),
    )


def test_valid_canonical_slice_is_still_admitted() -> None:
    event = _quote()
    result = TimeSlice(0, NOW, (event,))
    assert result.events == (event,)
    assert result.at == NOW


def test_behavioral_tuple_container_never_iterated_during_admission() -> None:
    class UntrustedTuple(tuple):
        def __iter__(self):
            raise AssertionError("caller-controlled __iter__ executed")

    with pytest.raises(TradingResearchError, match="behavioral paper carrier"):
        TimeSlice(0, NOW, UntrustedTuple((_quote(),)))


def test_forged_nested_decimal_does_not_run_comparison_callbacks() -> None:
    class BehavioralDecimal(Decimal):
        def __lt__(self, other):
            raise AssertionError("caller-controlled price comparison executed")

        def __gt__(self, other):
            raise AssertionError("caller-controlled price comparison executed")

    event = _quote()
    object.__setattr__(event, "bid", BehavioralDecimal("99"))
    with pytest.raises(TradingResearchError, match="behavioral paper carrier"):
        TimeSlice(0, NOW, (event,))


def test_forged_nested_event_time_rejected_before_attribute_dereference() -> None:
    event = _quote()
    object.__setattr__(event, "time", object())
    with pytest.raises(TradingResearchError, match="behavioral paper carrier"):
        TimeSlice(0, NOW, (event,))


def test_behavioral_timestamp_subclass_rejected_before_timezone_callbacks() -> None:
    class BehavioralTime(datetime):
        def utcoffset(self):
            raise AssertionError("caller-controlled datetime callback executed")

    at = BehavioralTime(2026, 1, 1, tzinfo=UTC)
    with pytest.raises(TradingResearchError, match="behavioral paper carrier"):
        TimeSlice(0, at, ())


def test_custom_tzinfo_rejected_before_utc_conversion() -> None:
    class BehavioralZone(tzinfo):
        def utcoffset(self, dt):
            raise AssertionError("caller-controlled timezone callback executed")

        def dst(self, dt):
            return timedelta(0)

        def tzname(self, dt):
            return "BEHAVIORAL"

    at = datetime(2026, 1, 1, tzinfo=BehavioralZone())
    with pytest.raises(TradingResearchError, match="unsupported paper carrier timezone"):
        TimeSlice(0, at, ())
