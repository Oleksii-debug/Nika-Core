from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from nika_core.data.sqlite import SQLiteStore
from nika_core.trading_research.accounting import PortfolioLedger
from nika_core.trading_research.contracts import EventTime, Instrument, Quote, Venue
from nika_core.trading_research.orders import (
    ExecutionPolicy,
    OrderIntent,
    OrderState,
    OrderType,
    RiskApprovedOrder,
    Side,
    SimulatedFill,
)
from nika_core.trading_research.persistence import TradingStateRepository
from nika_core.trading_research.replay import SimulationExecutionEngine, TimeSlice

NOW = datetime(2026, 1, 1, tzinfo=UTC)
INSTRUMENT = Instrument("TEST", Venue("SIM", "UTC"), "USD")


def _quote(at: datetime, *, bid: str = "99", ask: str = "101", size: str = "10") -> Quote:
    return Quote(
        INSTRUMENT,
        EventTime(at, at, at),
        Decimal(bid),
        Decimal(ask),
        Decimal(size),
        Decimal(size),
    )


def _approved(*, submitted_slice: int = 0, approved_slice: int = 0) -> RiskApprovedOrder:
    intent = OrderIntent(
        "intent-1",
        INSTRUMENT,
        Side.BUY,
        OrderType.MARKET,
        Decimal(5),
        NOW,
        submitted_slice,
    )
    return RiskApprovedOrder(
        "approval-1",
        intent,
        NOW,
        approved_slice,
        ExecutionPolicy("v1"),
    )


def test_new_order_cannot_fill_on_same_slice_even_with_zero_latency() -> None:
    update = SimulationExecutionEngine().execute(
        _approved(), TimeSlice(0, NOW, (_quote(NOW),))
    )
    assert update.state is OrderState.PENDING
    assert update.fill is None
    assert "same-slice" in update.reason


def test_approved_order_fills_only_on_later_slice_and_uses_quote_ask_for_buy() -> None:
    order = _approved()
    update = SimulationExecutionEngine().execute(
        order, TimeSlice(1, NOW, (_quote(NOW),))
    )
    assert update.state is OrderState.FILLED
    assert update.remaining_quantity == 0
    assert update.fill is not None
    assert update.fill.price == Decimal(101)
    assert update.fill.quantity == Decimal(5)


def test_partial_fill_is_bounded_by_explicit_liquidity_fraction() -> None:
    intent = OrderIntent(
        "partial",
        INSTRUMENT,
        Side.BUY,
        OrderType.MARKET,
        Decimal(10),
        NOW,
        0,
    )
    order = RiskApprovedOrder(
        "risk:partial",
        intent,
        NOW,
        0,
        ExecutionPolicy("half", max_fill_fraction=Decimal("0.5")),
    )
    update = SimulationExecutionEngine().execute(
        order, TimeSlice(1, NOW, (_quote(NOW, size="8"),))
    )
    assert update.state is OrderState.PARTIALLY_FILLED
    assert update.fill is not None
    assert update.fill.quantity == Decimal(4)
    assert update.remaining_quantity == Decimal(6)


def test_limit_order_does_not_fill_when_quote_does_not_cross() -> None:
    intent = OrderIntent(
        "limit",
        INSTRUMENT,
        Side.BUY,
        OrderType.LIMIT,
        Decimal(1),
        NOW,
        0,
        Decimal(100),
    )
    order = RiskApprovedOrder("risk:limit", intent, NOW, 0, ExecutionPolicy("v1"))
    update = SimulationExecutionEngine().execute(
        order, TimeSlice(1, NOW, (_quote(NOW, ask="101"),))
    )
    assert update.state is OrderState.ACTIVE
    assert update.fill is None


def test_committed_fill_and_account_are_exactly_once_after_restart(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repo = TradingStateRepository(store)
    repo.initialize()
    ledger = PortfolioLedger(Decimal(1000))
    fill = SimulatedFill(
        "fill-1",
        "approval-1",
        "intent-1",
        INSTRUMENT,
        Side.BUY,
        Decimal(2),
        Decimal(100),
        Decimal(1),
        NOW,
        1,
    )
    ledger.apply_fill(fill)
    snapshot = ledger.snapshot({INSTRUMENT.instrument_id: Decimal(100)})
    assert repo.commit_fill_and_account(fill, snapshot) is True

    restarted = TradingStateRepository(SQLiteStore(tmp_path / "nika.db"))
    restarted.initialize()
    assert restarted.has_fill(fill.fill_id)
    assert restarted.fill_count() == 1
    assert restarted.commit_fill_and_account(fill, snapshot) is False
    assert restarted.fill_count() == 1
    payload = restarted.account_payload()
    assert payload is not None
    assert payload["cash"] == "799"


def test_crash_before_commit_leaves_no_partial_fill_or_account_state(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repo = TradingStateRepository(store)
    repo.initialize()

    restarted = TradingStateRepository(SQLiteStore(tmp_path / "nika.db"))
    restarted.initialize()
    assert restarted.fill_count() == 0
    assert restarted.account_payload() is None
