"""Plan 5 §1: no-lookahead, deterministic event time waves, no second runtime."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from nika_core.trading_research import (
    Dataset,
    EventTime,
    Instrument,
    Provenance,
    Quote,
    Venue,
)
from nika_core.trading_research.contracts import FutureAccessError, TradingResearchError
from nika_core.trading_research.time_waves import group_visible_time_waves


AT = datetime(2026, 1, 1, 12, tzinfo=UTC)


def quote(
    event_minute: int,
    available_minute: int,
    *,
    name: str = "A",
    venue_timezone: str = "UTC",
) -> Quote:
    return Quote(
        Instrument(name, Venue("PAPER", venue_timezone), "USD"),
        EventTime(AT + timedelta(minutes=event_minute), AT + timedelta(minutes=available_minute)),
        Decimal("1.1"),
        Decimal("1.2"),
    )


def view(events: list[Quote], *, at: datetime = AT):
    dataset = Dataset("paper", "v1", events, Provenance("test fixture"))
    return dataset.temporal_view(at)


def test_prematch_events_use_event_time_and_do_not_leak_future_quotes() -> None:
    early = quote(30, -30)
    future_data = quote(30, 5, name="B")
    result = group_visible_time_waves(view([future_data, early]), window_minutes=30)
    assert len(result) == 1
    assert result[0].start_at == AT + timedelta(minutes=30)
    assert result[0].end_at == AT + timedelta(minutes=60)
    assert result[0].event_count == 1
    assert result[0].instruments == (("PAPER", "UTC", "A", "USD"),)


def test_wave_order_is_deterministic_and_portfolio_identity_is_complete() -> None:
    events = [quote(90, -1, name="B"), quote(32, -2), quote(50, -1, venue_timezone="Europe/Bratislava")]
    first = group_visible_time_waves(view(events), window_minutes=30)
    second = group_visible_time_waves(view(list(reversed(events))), window_minutes=30)
    assert first == second
    assert [(wave.start_at, wave.event_count) for wave in first] == [
        (AT + timedelta(minutes=30), 2),
        (AT + timedelta(minutes=90), 1),
    ]
    assert first[0].instruments == (
        ("PAPER", "Europe/Bratislava", "A", "USD"),
        ("PAPER", "UTC", "A", "USD"),
    )


@pytest.mark.parametrize("minutes", [True, 0, -1, 17, 1441, 1.5])
def test_bad_window_sizes_fail_before_access(minutes: object) -> None:
    with pytest.raises(TradingResearchError):
        group_visible_time_waves(view([]), window_minutes=minutes)


@pytest.mark.parametrize("max_events", [True, 0, -1, 100001, 0.5])
def test_invalid_budgets_fail_closed(max_events: object) -> None:
    with pytest.raises(TradingResearchError):
        group_visible_time_waves(view([]), max_events=max_events)


def test_visible_event_budget_is_bounded_and_empty_view_is_explicit() -> None:
    assert group_visible_time_waves(view([])) == ()
    with pytest.raises(TradingResearchError):
        group_visible_time_waves(view([quote(0, -1), quote(10, -1)]), max_events=1)


def test_only_canonical_temporal_view_accepted() -> None:
    with pytest.raises(TypeError):
        group_visible_time_waves([quote(0, -1)])


def test_mutated_availability_after_view_creation_fails_closed() -> None:
    event = quote(15, -5)
    temporal = view([event])
    object.__setattr__(event.time, "available_at", AT + timedelta(days=1))
    with pytest.raises(FutureAccessError):
        group_visible_time_waves(temporal)


def test_behavioral_identity_carrier_cannot_run_callbacks() -> None:
    event = quote(15, -5)
    temporal = view([event])
    called: list[str] = []

    class EvilString(str):
        def __str__(self):
            called.append("str")
            return "PAPER"

        def __hash__(self):
            called.append("hash")
            return 0

    object.__setattr__(event.instrument.venue, "venue_id", EvilString("PAPER"))
    with pytest.raises(TradingResearchError):
        group_visible_time_waves(temporal)
    assert called == []


@pytest.mark.parametrize(
    "source_at",
    [
        AT + timedelta(minutes=1),
        datetime(2026, 1, 1, 11),  # naive timestamp
        "2026-01-01T11:00:00Z",
    ],
)
def test_source_time_mutation_after_view_creation_fails_closed(source_at: object) -> None:
    event = quote(30, -5)
    temporal = view([event])
    object.__setattr__(event.time, "source_at", source_at)
    with pytest.raises(TradingResearchError, match="source-time provenance"):
        group_visible_time_waves(temporal)


def test_source_must_not_follow_available_after_view_creation() -> None:
    event = quote(30, -5)
    temporal = view([event])
    object.__setattr__(event.time, "source_at", AT - timedelta(minutes=4))
    with pytest.raises(TradingResearchError, match="source-time provenance"):
        group_visible_time_waves(temporal)


def test_valid_utc_source_time_preserves_prematch_wave() -> None:
    event = quote(30, -5)
    temporal = view([event])
    object.__setattr__(event.time, "source_at", AT - timedelta(minutes=10))
    waves = group_visible_time_waves(temporal, window_minutes=30)
    assert len(waves) == 1
    assert waves[0].event_count == 1
    assert waves[0].start_at == AT + timedelta(minutes=30)


def test_report_values_are_detached_from_mutable_source_objects() -> None:
    event = quote(20, -10)
    result = group_visible_time_waves(view([event]), window_minutes=30)
    object.__setattr__(event.instrument, "instrument_id", "ALTERED")
    assert result[0].instruments == (("PAPER", "UTC", "A", "USD"),)
