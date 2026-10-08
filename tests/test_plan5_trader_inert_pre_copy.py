"""Plan 5 §1: no behavioral nested data may execute before paper admission."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta, tzinfo
from decimal import Decimal

import pytest

from nika_core.trading_research.accounting import PortfolioLedger
from nika_core.trading_research.contracts import (
    EventTime, Instrument, OddsSnapshot, Quote, TradingResearchError, Venue,
)
from nika_core.trading_research.orders import (
    ExecutionPolicy, OrderAuthority, OrderIntent, OrderState, OrderType,
    RiskApprovedOrder, Side,
)
from nika_core.trading_research.replay import (
    ReplayBook, SimulationExecutionEngine, TimeSlice,
)


NOW = datetime(2026, 1, 1, tzinfo=UTC)
INSTRUMENT = Instrument("INERT", Venue("PAPER", "UTC"), "USD")


def _order() -> RiskApprovedOrder:
    return RiskApprovedOrder(
        "approval", OrderIntent(
            "intent", INSTRUMENT, Side.BUY, OrderType.MARKET,
            Decimal("2"), NOW, 0,
        ), OrderAuthority("trader", "research", "order", NOW, 0),
        NOW, 0, ExecutionPolicy("paper"),
    )


def _slice() -> TimeSlice:
    return TimeSlice(
        1, NOW, (
            Quote(
                INSTRUMENT, EventTime(NOW, NOW, NOW),
                Decimal("99"), Decimal("101"), Decimal("10"), Decimal("10"),
            ),
        ),
    )


def test_behavioral_decimal_does_not_execute_deepcopy_before_paper_accounting() -> None:
    invoked: list[str] = []

    class BehavioralDecimal(Decimal):
        def __deepcopy__(self, memo: dict) -> object:
            invoked.append("copy")
            raise AssertionError("hostile decimal deepcopy called")

    slice_ = _slice()
    object.__setattr__(slice_.events[0], "ask", BehavioralDecimal("101"))
    book = ReplayBook(PortfolioLedger(Decimal("1000")))
    with pytest.raises(TradingResearchError, match="behavioral paper carrier"):
        book.process_existing_order(_order(), slice_)
    assert invoked == []
    assert book.ledger.cash == Decimal("1000")
    assert book._last_slice == {}
    # A rejection must not poison later clean replays.
    assert book.process_existing_order(_order(), _slice()).state is OrderState.FILLED


def test_direct_simulator_rejects_behavioral_policy_latency_before_copy() -> None:
    invoked: list[str] = []

    class BehavioralDelay(timedelta):
        def __deepcopy__(self, memo: dict) -> object:
            invoked.append("copy")
            raise AssertionError("hostile policy deepcopy called")

    order = _order()
    object.__setattr__(order.policy, "latency", BehavioralDelay(seconds=0))
    with pytest.raises(TradingResearchError, match="behavioral paper carrier"):
        SimulationExecutionEngine().execute(order, _slice())
    assert invoked == []


def test_behavioral_timezone_is_rejected_before_deepcopy() -> None:
    invoked: list[str] = []

    class BehavioralZone(tzinfo):
        def utcoffset(self, dt: datetime | None) -> timedelta:
            return timedelta(0)

        def dst(self, dt: datetime | None) -> timedelta:
            return timedelta(0)

        def tzname(self, dt: datetime | None) -> str:
            return "hostile"

        def __deepcopy__(self, memo: dict) -> object:
            invoked.append("copy")
            raise AssertionError("hostile timezone deepcopy called")

    slice_ = _slice()
    object.__setattr__(
        slice_.events[0].time,
        "event_at",
        datetime(2026, 1, 1, tzinfo=BehavioralZone()),
    )
    with pytest.raises(TradingResearchError, match="unsupported paper carrier timezone"):
        SimulationExecutionEngine().execute(_order(), slice_)
    assert invoked == []


def test_behavioral_odds_mapping_rejected_without_items_iteration() -> None:
    invoked: list[str] = []

    class BehavioralDict(dict):
        def items(self):
            invoked.append("items")
            raise AssertionError("hostile odds iteration called")

    odds = OddsSnapshot(
        INSTRUMENT, EventTime(NOW, NOW, NOW), {"home": Decimal("2")},
    )
    slice_ = TimeSlice(1, NOW, (odds,))
    object.__setattr__(
        odds, "selections", BehavioralDict({"home": Decimal("2")}),
    )
    with pytest.raises(TradingResearchError, match="behavioral paper carrier"):
        SimulationExecutionEngine().execute(_order(), slice_)
    assert invoked == []


def test_standard_fixed_and_iana_timezones_remain_eligible() -> None:
    from zoneinfo import ZoneInfo

    order = _order()
    quote = _slice().events[0]
    time = quote.time
    object.__setattr__(
        time, "event_at", datetime(2026, 1, 1, tzinfo=ZoneInfo("UTC")),
    )
    result = SimulationExecutionEngine().execute(order, TimeSlice(1, NOW, (quote,)))
    assert result.state is OrderState.FILLED


def test_valid_mappingproxy_odds_still_passes_market_snapshot_admission() -> None:
    odds = OddsSnapshot(
        INSTRUMENT, EventTime(NOW, NOW, NOW), {"home": Decimal("2")},
    )
    result = SimulationExecutionEngine().execute(
        _order(), TimeSlice(1, NOW, (odds,)),
    )
    assert result.state is OrderState.ACTIVE
    assert result.fill is None
