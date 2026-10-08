"""Plan 5 §1: cancellation audit text must not alter paper authority or state."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from nika_core.trading_research.accounting import PortfolioLedger
from nika_core.trading_research.contracts import (
    EventTime,
    Instrument,
    Quote,
    TradingResearchError,
    Venue,
)
from nika_core.trading_research.orders import (
    ExecutionPolicy,
    OrderAuthority,
    OrderIntent,
    OrderState,
    OrderType,
    RiskApprovedOrder,
    Side,
)
from nika_core.trading_research.replay import ReplayBook, TimeSlice

NOW = datetime(2026, 1, 1, tzinfo=UTC)
INSTRUMENT = Instrument("TEST", Venue("SIM", "UTC"), "USD")


def _order(*, quantity: str = "2", fraction: str = "1") -> RiskApprovedOrder:
    return RiskApprovedOrder(
        "approval",
        OrderIntent(
            "intent", INSTRUMENT, Side.BUY, OrderType.MARKET,
            Decimal(quantity), NOW, 0,
        ),
        OrderAuthority("workspace", "run", "order", NOW, 0),
        NOW, 0, ExecutionPolicy("paper", max_fill_fraction=Decimal(fraction)),
    )


def _slice() -> TimeSlice:
    return TimeSlice(
        1, NOW,
        (
            Quote(
                INSTRUMENT, EventTime(NOW, NOW, NOW),
                Decimal("99"), Decimal("101"), Decimal("10"), Decimal("10"),
            ),
        ),
    )


@pytest.mark.parametrize(
    "reason",
    [None, 1, True, "", "   ", "line one\\nline two", "escape\\x1bsequence",
     "spoof\\u202etext", "x" * 513],
)
def test_invalid_cancel_reason_rejected_without_paper_state_effects(reason: object) -> None:
    book = ReplayBook(PortfolioLedger(Decimal("1000")))
    with pytest.raises(TradingResearchError, match="cancellation reason"):
        book.cancel(_order(), reason=reason)
    assert book.ledger.cash == Decimal("1000")
    assert book._remaining == {}
    assert book._terminal == {}
    assert book._accepted_orders == {}
    assert book._approval_keys == {}
    assert book._scope is None


def test_reason_subclass_callbacks_never_execute() -> None:
    callbacks: list[str] = []

    class BehavioralReason(str):
        def strip(self, *args, **kwargs):
            callbacks.append("strip")
            raise AssertionError("untrusted strip callback")

        def isprintable(self):
            callbacks.append("isprintable")
            raise AssertionError("untrusted printability callback")

        def encode(self, *args, **kwargs):
            callbacks.append("encode")
            raise AssertionError("untrusted encode callback")

    book = ReplayBook(PortfolioLedger(Decimal("1000")))
    with pytest.raises(TradingResearchError, match="cancellation reason"):
        book.cancel(_order(), reason=BehavioralReason("spoof"))
    assert callbacks == []
    assert book._terminal == {}
    assert book._scope is None


def test_valid_unicode_cancel_is_terminal_and_idempotent() -> None:
    book = ReplayBook(PortfolioLedger(Decimal("1000")))
    reason = "Скасовано оператором: тільки паперова симуляція"
    first = book.cancel(_order(), reason=reason)
    assert first.state is OrderState.CANCELLED
    assert first.reason == reason
    assert book._scope == ("workspace", "run")
    assert book.cancel(_order(), reason="safe retry") is first
    assert book.process_existing_order(_order(), _slice()) is first
    assert book.ledger.cash == Decimal("1000")


def test_invalid_reason_after_partial_fill_preserves_recovery() -> None:
    book = ReplayBook(PortfolioLedger(Decimal("1000")))
    order = _order(quantity="2", fraction="0.5")
    first = book.process_existing_order(order, _slice())
    assert first.state is OrderState.PARTIALLY_FILLED
    old_cash = book.ledger.cash
    old_remaining = dict(book._remaining)
    old_last_slice = dict(book._last_slice)
    old_scope = book._scope
    with pytest.raises(TradingResearchError, match="cancellation reason"):
        book.cancel(order, reason="forged\\noperator log entry")
    assert book.ledger.cash == old_cash
    assert book._remaining == old_remaining
    assert book._last_slice == old_last_slice
    assert book._scope == old_scope
    assert book._terminal == {}
    cancelled = book.cancel(order, reason="operator stopped paper session")
    assert cancelled.state is OrderState.CANCELLED
    assert cancelled.remaining_quantity == Decimal("1")
    assert book.process_existing_order(order, _slice()) is cancelled
    assert book.ledger.cash == old_cash


def test_bounded_ascii_reason_at_limit_is_accepted() -> None:
    book = ReplayBook(PortfolioLedger(Decimal("1000")))
    result = book.cancel(_order(), reason="a" * 512)
    assert result.state is OrderState.CANCELLED
    assert len(result.reason) == 512
