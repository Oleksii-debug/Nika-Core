"""Market entity and timestamp admission must fail closed before hashing or replay."""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from nika_core.trading_research import (
    Bar,
    EventTime,
    Instrument,
    OddsSnapshot,
    OutcomeSettlement,
    Provenance,
    Quote,
    Tick,
    TradingResearchError,
    Venue,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)
VENUE = Venue("sim", "UTC")
INSTRUMENT = Instrument("sim", VENUE, "usd")
TIME = EventTime(NOW, NOW)


@pytest.mark.parametrize("bad", (None, 1, True, b"sim", "", "  "))
def test_venue_rejects_invalid_identity_with_domain_error(bad: object) -> None:
    with pytest.raises(TradingResearchError, match="venue_id"):
        Venue(bad)


@pytest.mark.parametrize("bad", (None, 1, True, b"UTC", "", "Not/A_Zone"))
def test_venue_rejects_invalid_timezone_with_domain_error(bad: object) -> None:
    with pytest.raises(TradingResearchError, match="timezone"):
        Venue("sim", bad)


@pytest.mark.parametrize("bad", (None, 1, True, b"sim", "", "  "))
def test_instrument_rejects_invalid_identity_with_domain_error(bad: object) -> None:
    with pytest.raises(TradingResearchError, match="instrument_id"):
        Instrument(bad, VENUE, "USD")


@pytest.mark.parametrize("bad", (None, 1, True, "sim", {}))
def test_instrument_rejects_invalid_venue_with_domain_error(bad: object) -> None:
    with pytest.raises(TradingResearchError, match="venue"):
        Instrument("sim", bad, "USD")


@pytest.mark.parametrize("bad", (None, 1, True, b"USD", "грн", "EU", "123"))
def test_instrument_rejects_invalid_currency_with_domain_error(bad: object) -> None:
    with pytest.raises(TradingResearchError, match="currency"):
        Instrument("sim", VENUE, bad)


@pytest.mark.parametrize("bad", (1, True, "2026-01-01", None))
@pytest.mark.parametrize("position", ("event_at", "available_at", "source_at"))
def test_event_time_rejects_nondatetime_values(position: str, bad: object) -> None:
    if position == "source_at" and bad is None:
        pytest.skip("None is legal for optional source_at")
    values = {"event_at": NOW, "available_at": NOW, "source_at": NOW}
    values[position] = bad
    with pytest.raises(TradingResearchError, match=position):
        EventTime(**values)


def test_event_time_normalizes_valid_offsets_and_rejects_naive_values() -> None:
    offset = datetime(2026, 1, 1, 2, tzinfo=UTC)
    event = EventTime(offset, offset + timedelta(minutes=1))
    assert event.event_at == NOW + timedelta(hours=2)
    with pytest.raises(TradingResearchError, match="event_at must be timezone-aware"):
        EventTime(datetime(2026, 1, 1), NOW)
    # Conversion at the representable boundary must produce a domain error,
    # not leak OverflowError from datetime.astimezone().
    near_min = datetime.min.replace(tzinfo=timezone(timedelta(hours=14)))
    with pytest.raises(TradingResearchError, match="event_at must be a valid aware datetime"):
        EventTime(near_min, NOW)


@pytest.mark.parametrize("bad", (None, 1, True, b"home", "", "  "))
def test_outcome_rejects_invalid_identity_with_domain_error(bad: object) -> None:
    with pytest.raises(TradingResearchError, match="outcome"):
        OutcomeSettlement(INSTRUMENT, TIME, bad, Decimal(0))


@pytest.mark.parametrize("bad", (None, 1, True, b"source", "", "  "))
def test_provenance_rejects_invalid_source_id(bad: object) -> None:
    with pytest.raises(TradingResearchError, match="source_id"):
        Provenance(bad, acquired_at=NOW)


@pytest.mark.parametrize("field", ("source_uri", "license_id"))
@pytest.mark.parametrize("bad", (1, True, b"opaque", {}))
def test_provenance_rejects_invalid_optional_evidence(field: str, bad: object) -> None:
    with pytest.raises(TradingResearchError, match=field):
        Provenance("source", acquired_at=NOW, **{field: bad})


@pytest.mark.parametrize("bad", (None, True, 1, "home", [("home", "2")]))
def test_odds_rejects_nonmapping_selections(bad: object) -> None:
    with pytest.raises(TradingResearchError, match="mapping"):
        OddsSnapshot(INSTRUMENT, TIME, bad)


class InconsistentMapping(Mapping):
    """Simulate a changing external mapping across key and item reads."""

    def __iter__(self):
        return iter(("home",))

    def __len__(self):
        return 1

    def __getitem__(self, key):
        return "2"

    def items(self):
        return iter(((1, "2"),))


class DuplicateItems(Mapping):
    def __iter__(self):
        return iter(("home",))

    def __len__(self):
        return 1

    def __getitem__(self, key):
        return "2"

    def items(self):
        return iter((("home", "2"), ("home", "3")))


def test_odds_validates_the_same_items_it_snapshots() -> None:
    with pytest.raises(TradingResearchError, match="selection keys"):
        OddsSnapshot(INSTRUMENT, TIME, InconsistentMapping())
    with pytest.raises(TradingResearchError, match="unique"):
        OddsSnapshot(INSTRUMENT, TIME, DuplicateItems())


def test_valid_market_evidence_preserves_unicode_and_detaches_mapping() -> None:
    venue = Venue("Київ", "Europe/Kyiv")
    instrument = Instrument("Пшениця", venue, "uah")
    selections = {"перемога": "2.25"}
    snapshot = OddsSnapshot(instrument, TIME, selections)
    selections["перемога"] = "100.00"
    assert snapshot.selections["перемога"] == Decimal("2.25")
    assert instrument.currency == "UAH"
    assert Provenance("Джерело", source_uri=None, acquired_at=NOW).source_id == "Джерело"

@pytest.mark.parametrize("event", ("bar", "tick", "quote", "odds", "settlement"))
@pytest.mark.parametrize("field,bad", (("instrument", None), ("instrument", "sim"),
                                      ("time", None), ("time", "2026-01-01")))
def test_market_events_reject_invalid_nested_authorities(
    event: str, field: str, bad: object
) -> None:
    instrument = bad if field == "instrument" else INSTRUMENT
    time = bad if field == "time" else TIME
    with pytest.raises(TradingResearchError, match=f"event {field}"):
        if event == "bar":
            Bar(instrument, time, Decimal(10), Decimal(11), Decimal(9), Decimal(10), Decimal(1))
        elif event == "tick":
            Tick(instrument, time, Decimal(10), Decimal(1))
        elif event == "quote":
            Quote(instrument, time, Decimal(9), Decimal(11))
        elif event == "odds":
            OddsSnapshot(instrument, time, {"home": Decimal(2)})
        else:
            OutcomeSettlement(instrument, time, "home", Decimal(0))
