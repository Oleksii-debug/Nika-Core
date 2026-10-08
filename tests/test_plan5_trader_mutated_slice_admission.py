from __future__ import annotations

from datetime import UTC, datetime, timedelta
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
INSTRUMENT = Instrument("TEST", Venue("SIM", "UTC"), "USD")


def _quote() -> Quote:
    return Quote(
        INSTRUMENT, EventTime(NOW, NOW, NOW),
        Decimal("99"), Decimal("101"), Decimal("10"), Decimal("10"),
    )


def _approved() -> RiskApprovedOrder:
    return RiskApprovedOrder(
        "approval",
        OrderIntent("intent", INSTRUMENT, Side.BUY, OrderType.MARKET,
                    Decimal("2"), NOW, 0),
        OrderAuthority("workspace", "run", "order", NOW, 0),
        NOW, 0, ExecutionPolicy("paper"),
    )


def test_normal_detached_paper_replay_remains_executable() -> None:
    book = ReplayBook(PortfolioLedger(Decimal("1000")))
    update = book.process_existing_order(_approved(), TimeSlice(1, NOW, (_quote(),)))
    assert update.state is OrderState.FILLED
    assert update.fill is not None and update.fill.quantity == Decimal("2")
    assert book.ledger.cash == Decimal("798")


def test_future_time_mutation_is_rejected_before_account_effect_and_retryable() -> None:
    book = ReplayBook(PortfolioLedger(Decimal("1000")))
    quote = _quote()
    slice_ = TimeSlice(1, NOW, (quote,))
    object.__setattr__(quote.time, "available_at", NOW + timedelta(minutes=1))

    with pytest.raises(TradingResearchError, match="future-unavailable"):
        book.process_existing_order(_approved(), slice_)
    assert book.ledger.cash == Decimal("1000")
    assert book.ledger.position(INSTRUMENT).quantity == 0
    assert book._remaining == {}
    assert book._last_slice == {}

    valid = TimeSlice(1, NOW, (_quote(),))
    result = book.process_existing_order(_approved(), valid)
    assert result.state is OrderState.FILLED
    assert book.ledger.cash == Decimal("798")


def test_forged_quote_fields_are_readmitted_before_paper_execution() -> None:
    quote = _quote()
    slice_ = TimeSlice(1, NOW, (quote,))
    object.__setattr__(quote, "ask", Decimal("-1"))
    book = ReplayBook(PortfolioLedger(Decimal("1000")))
    with pytest.raises(TradingResearchError, match="quote requires"):
        book.process_existing_order(_approved(), slice_)
    assert book.ledger.cash == Decimal("1000")
    assert not book._terminal


def test_direct_engine_cannot_consume_mutated_future_market_data() -> None:
    quote = _quote()
    slice_ = TimeSlice(1, NOW, (quote,))
    object.__setattr__(quote.time, "available_at", NOW + timedelta(minutes=1))
    with pytest.raises(TradingResearchError, match="future-unavailable"):
        SimulationExecutionEngine().execute(_approved(), slice_)


def test_mapping_backed_odds_still_admit_without_deepcopying_mapping_proxy() -> None:
    odds = OddsSnapshot(INSTRUMENT, EventTime(NOW, NOW, NOW), {"win": Decimal("2")})
    slice_ = TimeSlice(1, NOW, (odds,))
    update = SimulationExecutionEngine().execute(_approved(), slice_)
    assert update.state is OrderState.ACTIVE
    assert update.fill is None


def test_type_forged_event_rejected_without_mutating_account_state() -> None:
    slice_ = TimeSlice(1, NOW, (_quote(),))
    object.__setattr__(slice_, "events", (object(),))
    book = ReplayBook(PortfolioLedger(Decimal("1000")))
    with pytest.raises(TradingResearchError, match="unsupported paper market event"):
        book.process_existing_order(_approved(), slice_)
    assert book.ledger.cash == Decimal("1000")
    assert not book._last_slice


def test_accepted_order_is_detached_from_later_caller_mutation() -> None:
    book = ReplayBook(PortfolioLedger(Decimal("1000")))
    order = _approved()
    pending = book.process_existing_order(order, TimeSlice(0, NOW, ()))
    assert pending.state is OrderState.PENDING
    object.__setattr__(order.intent, "quantity", Decimal("100"))
    with pytest.raises(TradingResearchError, match="conflicting approved order"):
        book.process_existing_order(order, TimeSlice(1, NOW, (_quote(),)))
    assert book.ledger.cash == Decimal("1000")
    assert book._remaining[next(iter(book._remaining))] == Decimal("2")
