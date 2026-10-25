"""Reject corrupt numeric market evidence before it reaches causal datasets or replay."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from nika_core.trading_research import (
    Bar,
    EventTime,
    Instrument,
    OddsSnapshot,
    OutcomeSettlement,
    Quote,
    Tick,
    TradingResearchError,
    Venue,
)

AT = datetime(2026, 1, 1, tzinfo=UTC)
INSTRUMENT = Instrument("TEST", Venue("sim"), "USD")
TIME = EventTime(AT, AT)
BAD_NUMBERS = ("NaN", "sNaN", "Infinity", "-Infinity")


def bar(**overrides: object) -> Bar:
    values = {
        "open": Decimal(10),
        "high": Decimal(11),
        "low": Decimal(9),
        "close": Decimal(10),
        "volume": Decimal(0),
    }
    values.update(overrides)
    return Bar(INSTRUMENT, TIME, **values)


def tick(**overrides: object) -> Tick:
    values = {"price": Decimal(10), "size": Decimal(0)}
    values.update(overrides)
    return Tick(INSTRUMENT, TIME, **values)


def quote(**overrides: object) -> Quote:
    values = {
        "bid": Decimal(9),
        "ask": Decimal(11),
        "bid_size": Decimal(0),
        "ask_size": Decimal(0),
    }
    values.update(overrides)
    return Quote(INSTRUMENT, TIME, **values)


@pytest.mark.parametrize("field", ("open", "high", "low", "close", "volume"))
@pytest.mark.parametrize("bad", BAD_NUMBERS)
def test_bar_rejects_nonfinite_fields(field: str, bad: str) -> None:
    with pytest.raises(TradingResearchError, match="finite Decimal"):
        bar(**{field: Decimal(bad)})


@pytest.mark.parametrize("field", ("price", "size"))
@pytest.mark.parametrize("bad", BAD_NUMBERS)
def test_tick_rejects_nonfinite_fields(field: str, bad: str) -> None:
    with pytest.raises(TradingResearchError, match="finite Decimal"):
        tick(**{field: Decimal(bad)})


@pytest.mark.parametrize("field", ("bid", "ask", "bid_size", "ask_size"))
@pytest.mark.parametrize("bad", BAD_NUMBERS)
def test_quote_rejects_nonfinite_fields(field: str, bad: str) -> None:
    with pytest.raises(TradingResearchError, match="finite Decimal"):
        quote(**{field: Decimal(bad)})


@pytest.mark.parametrize("bad", BAD_NUMBERS)
def test_odds_rejects_nonfinite_fields(bad: str) -> None:
    with pytest.raises(TradingResearchError, match="odds"):
        OddsSnapshot(INSTRUMENT, TIME, {"home": Decimal(2), "away": Decimal(bad)})


@pytest.mark.parametrize("bad", ("invalid", None, object()))
def test_odds_rejects_invalid_values_with_domain_error(bad: object) -> None:
    with pytest.raises(TradingResearchError, match="odds"):
        OddsSnapshot(INSTRUMENT, TIME, {"home": bad})


@pytest.mark.parametrize("key", (None, 1, True, "", " ", "\t"))
def test_odds_rejects_nontext_or_blank_selection_identity(key: object) -> None:
    with pytest.raises(TradingResearchError, match="selection keys"):
        OddsSnapshot(INSTRUMENT, TIME, {key: Decimal(2)})


def test_odds_does_not_silently_collapse_text_and_nontext_aliases() -> None:
    with pytest.raises(TradingResearchError, match="selection keys"):
        OddsSnapshot(INSTRUMENT, TIME, {1: Decimal(2), "1": Decimal(3)})


@pytest.mark.parametrize("bad", BAD_NUMBERS)
def test_settlement_rejects_nonfinite_values(bad: str) -> None:
    with pytest.raises(TradingResearchError, match="finite Decimal"):
        OutcomeSettlement(INSTRUMENT, TIME, "home", Decimal(bad))


@pytest.mark.parametrize("bad", (True, False, -1, 1.5, "2", None))
@pytest.mark.parametrize("event", ("bar", "tick", "quote", "odds", "settlement"))
def test_all_market_events_require_exact_nonnegative_integer_sequence(
    event: str, bad: object
) -> None:
    with pytest.raises(TradingResearchError, match="source_sequence"):
        if event == "bar":
            Bar(INSTRUMENT, TIME, Decimal(10), Decimal(11), Decimal(9), Decimal(10), Decimal(1), bad)
        elif event == "tick":
            Tick(INSTRUMENT, TIME, Decimal(10), Decimal(1), bad)
        elif event == "quote":
            Quote(INSTRUMENT, TIME, Decimal(9), Decimal(11), Decimal(1), Decimal(1), bad)
        elif event == "odds":
            OddsSnapshot(INSTRUMENT, TIME, {"home": Decimal(2)}, bad)
        else:
            OutcomeSettlement(INSTRUMENT, TIME, "home", Decimal(0), bad)


@pytest.mark.parametrize("event", ("bar", "tick", "quote", "odds", "settlement"))
def test_all_market_events_accept_zero_sequence_and_finite_values(event: str) -> None:
    if event == "bar":
        result = bar()
    elif event == "tick":
        result = tick()
    elif event == "quote":
        result = quote()
    elif event == "odds":
        result = OddsSnapshot(INSTRUMENT, TIME, {"home": "1.8"})
        assert result.selections["home"] == Decimal("1.8")
    else:
        result = OutcomeSettlement(INSTRUMENT, TIME, "home", Decimal(-1))
    assert result.source_sequence == 0
