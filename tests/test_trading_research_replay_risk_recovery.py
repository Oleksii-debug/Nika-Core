from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.trading_research.accounting import PortfolioLedger
from nika_core.trading_research.contracts import (
    EventTime,
    Instrument,
    Quote,
    TradingResearchError,
    Venue,
)
from nika_core.trading_research.identity import instrument_identity
from nika_core.trading_research.orders import (
    ExecutionPolicy,
    OrderAuthority,
    OrderIntent,
    OrderState,
    OrderType,
    RiskApprovedOrder,
    Side,
    SimulatedFill,
)
from nika_core.trading_research.persistence import TradingStateRepository
from nika_core.trading_research.replay import (
    ReplayBook,
    ReplayPhase,
    SimulationExecutionEngine,
    TimeSlice,
)

NOW = datetime(2026, 1, 1, tzinfo=UTC)
INSTRUMENT = Instrument("TEST", Venue("SIM", "UTC"), "USD")


def _authority(
    order_id: str = "order-1",
    *,
    submitted_slice: int = 0,
    submitted_at: datetime = NOW,
) -> OrderAuthority:
    return OrderAuthority("workspace", "run", order_id, submitted_at, submitted_slice)


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
        _authority("order-1", submitted_slice=submitted_slice),
        NOW,
        approved_slice,
        ExecutionPolicy("v1"),
    )


def _fill(fill_id: str = "fill-1") -> SimulatedFill:
    return SimulatedFill(
        fill_id=fill_id,
        approval_id="approval-1",
        intent_id="intent-1",
        authority=_authority(),
        instrument=INSTRUMENT,
        side=Side.BUY,
        quantity=Decimal(2),
        price=Decimal(100),
        fee=Decimal(1),
        filled_at=NOW,
        filled_slice=1,
    )


def _snapshot(fill: SimulatedFill):
    ledger = PortfolioLedger(Decimal(1000))
    ledger.apply_fill(fill)
    return ledger.snapshot({instrument_identity(INSTRUMENT): Decimal(100)})


def test_replay_phase_order_is_binding_and_deterministic() -> None:
    assert tuple(ReplayPhase) == (
        ReplayPhase.MARKET_DATA,
        ReplayPhase.EXISTING_ORDERS,
        ReplayPhase.ACCOUNTING,
        ReplayPhase.STRATEGY,
        ReplayPhase.RISK,
        ReplayPhase.QUEUE_NEW_ORDERS,
    )


def test_time_slice_rejects_future_unavailable_market_data() -> None:
    future = NOW + timedelta(minutes=1)
    quote = Quote(
        INSTRUMENT,
        EventTime(NOW, future, future),
        Decimal(99),
        Decimal(101),
        Decimal(10),
        Decimal(10),
    )
    with pytest.raises(TradingResearchError, match="future-unavailable"):
        TimeSlice(1, NOW, (quote,))


def test_new_order_cannot_fill_on_same_slice_even_with_zero_latency() -> None:
    update = SimulationExecutionEngine().execute(_approved(), TimeSlice(0, NOW, (_quote(NOW),)))
    assert update.state is OrderState.PENDING
    assert update.fill is None
    assert "same-slice" in update.reason


def test_approved_order_fills_only_on_later_slice_and_uses_quote_ask_for_buy() -> None:
    order = _approved()
    update = SimulationExecutionEngine().execute(order, TimeSlice(1, NOW, (_quote(NOW),)))
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
        _authority("partial"),
        NOW,
        0,
        ExecutionPolicy("half", max_fill_fraction=Decimal("0.5")),
    )
    update = SimulationExecutionEngine().execute(
        order,
        TimeSlice(1, NOW, (_quote(NOW, size="8"),)),
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
    order = RiskApprovedOrder(
        "risk:limit",
        intent,
        _authority("limit"),
        NOW,
        0,
        ExecutionPolicy("v1"),
    )
    update = SimulationExecutionEngine().execute(
        order,
        TimeSlice(1, NOW, (_quote(NOW, ask="101"),)),
    )
    assert update.state is OrderState.ACTIVE
    assert update.fill is None


def test_expired_order_is_terminal_and_never_fills() -> None:
    intent = OrderIntent(
        "expiry",
        INSTRUMENT,
        Side.BUY,
        OrderType.MARKET,
        Decimal(1),
        NOW,
        0,
        expires_at=NOW + timedelta(seconds=1),
    )
    order = RiskApprovedOrder(
        "risk:expiry",
        intent,
        _authority("expiry"),
        NOW,
        0,
        ExecutionPolicy("v1"),
    )
    book = ReplayBook(PortfolioLedger(Decimal(1000)))
    expired_at = NOW + timedelta(seconds=1)
    first = book.process_existing_order(order, TimeSlice(1, expired_at, (_quote(expired_at),)))
    later = book.process_existing_order(
        order,
        TimeSlice(2, expired_at + timedelta(seconds=1), (_quote(expired_at),)),
    )
    assert first.state is OrderState.EXPIRED
    assert later is first
    assert book.ledger.cash == Decimal(1000)


def test_cancelled_order_remains_terminal_on_later_market_data() -> None:
    order = _approved()
    book = ReplayBook(PortfolioLedger(Decimal(1000)))
    cancelled = book.cancel(order)
    later = book.process_existing_order(order, TimeSlice(1, NOW, (_quote(NOW),)))
    assert cancelled.state is OrderState.CANCELLED
    assert later is cancelled
    assert book.ledger.cash == Decimal(1000)


def test_committed_fill_and_account_are_exactly_once_after_restart(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repo = TradingStateRepository(store)
    repo.initialize()
    fill = _fill()
    snapshot = _snapshot(fill)
    assert repo.commit_fill_and_account(fill, snapshot) is True

    restarted = TradingStateRepository(SQLiteStore(tmp_path / "nika.db"))
    restarted.initialize()
    assert restarted.has_fill("workspace", "run", fill.fill_id)
    assert restarted.fill_count("workspace", "run") == 1
    assert restarted.commit_fill_and_account(fill, snapshot) is False
    assert restarted.fill_count("workspace", "run") == 1
    payload = restarted.account_payload("workspace", "run")
    assert payload is not None
    assert payload["cash"] == "799"


def test_failed_account_write_rolls_back_fill_insert_atomically(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repo = TradingStateRepository(store)
    repo.initialize()
    with store.connection() as conn:
        conn.execute("DROP TABLE trading_research_run_account_state")

    fill = _fill("fill-rollback")
    with pytest.raises(sqlite3.OperationalError):
        repo.commit_fill_and_account(fill, _snapshot(fill))

    assert repo.fill_count("workspace", "run") == 0
    assert repo.has_fill("workspace", "run", fill.fill_id) is False


def test_crash_before_commit_leaves_no_partial_fill_or_account_state(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repo = TradingStateRepository(store)
    repo.initialize()

    restarted = TradingStateRepository(SQLiteStore(tmp_path / "nika.db"))
    restarted.initialize()
    assert restarted.fill_count("workspace", "run") == 0
    assert restarted.account_payload("workspace", "run") is None

@pytest.mark.parametrize("corruption", ["invalid-payload", "missing-fill"])
def test_later_fill_cannot_erase_corrupt_prior_account(tmp_path, corruption: str) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repo = TradingStateRepository(store)
    repo.initialize()
    first = _fill("fill-first")
    assert repo.commit_fill_and_account(first, _snapshot(first))

    with store.connection() as conn:
        if corruption == "invalid-payload":
            conn.execute(
                "UPDATE trading_research_run_account_state SET payload = ? "
                "WHERE workspace_id = ? AND run_id = ?",
                ("{", "workspace", "run"),
            )
        else:
            conn.execute(
                "UPDATE trading_research_run_account_state SET last_fill_id = ? "
                "WHERE workspace_id = ? AND run_id = ?",
                ("missing-fill", "workspace", "run"),
            )
        prior_row = conn.execute(
            "SELECT payload, last_fill_id FROM trading_research_run_account_state "
            "WHERE workspace_id = ? AND run_id = ?",
            ("workspace", "run"),
        ).fetchone()
        prior_values = (prior_row["payload"], prior_row["last_fill_id"])

    later = _fill("fill-later")
    with pytest.raises(RuntimeError):
        repo.commit_fill_and_account(later, _snapshot(later))
    assert repo.fill_count("workspace", "run") == 1
    assert not repo.has_fill("workspace", "run", later.fill_id)
    with store.connection() as conn:
        unchanged = conn.execute(
            "SELECT payload, last_fill_id FROM trading_research_run_account_state "
            "WHERE workspace_id = ? AND run_id = ?",
            ("workspace", "run"),
        ).fetchone()
    assert (unchanged["payload"], unchanged["last_fill_id"]) == prior_values


def test_later_fill_accepts_valid_previous_account_and_survives_restart(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repo = TradingStateRepository(store)
    repo.initialize()
    ledger = PortfolioLedger(Decimal(1000))
    first = _fill("first")
    ledger.apply_fill(first)
    assert repo.commit_fill_and_account(
        first, ledger.snapshot({instrument_identity(INSTRUMENT): Decimal(100)})
    )
    second = _fill("second")
    ledger.apply_fill(second)
    assert repo.commit_fill_and_account(
        second, ledger.snapshot({instrument_identity(INSTRUMENT): Decimal(100)})
    )
    restarted = TradingStateRepository(SQLiteStore(tmp_path / "nika.db"))
    restarted.initialize()
    assert restarted.fill_count("workspace", "run") == 2
    payload = restarted.account_payload("workspace", "run")
    assert payload is not None
    assert payload["cash"] == "598"
    assert payload["fees"] == "2"

def test_replay_keeps_remaining_order_after_accounting_rejection() -> None:
    class RejectingLedger(PortfolioLedger):
        def apply_fill(self, fill: SimulatedFill) -> None:
            raise TradingResearchError("injected account failure")

    order = _approved()
    book = ReplayBook(RejectingLedger(Decimal(1000)))
    time_slice = TimeSlice(1, NOW, (_quote(NOW),))
    with pytest.raises(TradingResearchError, match="injected account failure"):
        book.process_existing_order(order, time_slice)

    # Simulate repair/restart of the failed accounting adapter: the same order
    # and full remaining quantity must still be replayable exactly once.
    book.ledger = PortfolioLedger(Decimal(1000))
    update = book.process_existing_order(order, time_slice)
    assert update.state is OrderState.FILLED
    assert update.fill is not None
    assert update.fill.quantity == Decimal(5)
    assert book.ledger.cash == Decimal(495)
    assert book.process_existing_order(order, time_slice) is update
    assert book.ledger.cash == Decimal(495)


def test_partial_fill_cannot_consume_same_slice_twice_or_lose_accounting() -> None:
    intent = OrderIntent(
        "slice-intent", INSTRUMENT, Side.BUY, OrderType.MARKET,
        Decimal(5), NOW, 0,
    )
    order = RiskApprovedOrder(
        "slice-approval", intent, _authority("slice-order"), NOW, 0,
        ExecutionPolicy("half", max_fill_fraction=Decimal("0.5")),
    )
    book = ReplayBook(PortfolioLedger(Decimal(1000)))
    first_slice = TimeSlice(1, NOW, (_quote(NOW, size="4"),))
    first = book.process_existing_order(order, first_slice)
    assert first.state is OrderState.PARTIALLY_FILLED
    assert first.remaining_quantity == Decimal(3)
    assert book.ledger.position(INSTRUMENT).quantity == Decimal(2)
    assert book.ledger.cash == Decimal(798)

    # Replayed equal data is idempotent: no phantom remaining-quantity debit.
    assert book.process_existing_order(
        order, TimeSlice(1, NOW, (_quote(NOW, size="4"),))
    ) is first
    assert book.ledger.position(INSTRUMENT).quantity == Decimal(2)
    assert book.ledger.cash == Decimal(798)

    with pytest.raises(TradingResearchError, match="conflicting same-slice"):
        book.process_existing_order(
            order, TimeSlice(1, NOW, (_quote(NOW, ask="102", size="4"),))
        )
    assert book.ledger.cash == Decimal(798)

    next_at = NOW + timedelta(seconds=1)
    second = book.process_existing_order(
        order, TimeSlice(2, next_at, (_quote(next_at, size="4"),))
    )
    assert second.remaining_quantity == Decimal(1)
    assert book.ledger.position(INSTRUMENT).quantity == Decimal(4)
    assert book.ledger.cash == Decimal(596)
    with pytest.raises(TradingResearchError, match="cannot move backwards"):
        book.process_existing_order(order, first_slice)
    assert book.ledger.cash == Decimal(596)

    final_at = NOW + timedelta(seconds=2)
    third = book.process_existing_order(
        order, TimeSlice(3, final_at, (_quote(final_at, size="4"),))
    )
    assert third.state is OrderState.FILLED
    assert third.remaining_quantity == Decimal(0)
    assert book.ledger.position(INSTRUMENT).quantity == Decimal(5)
    assert book.ledger.cash == Decimal(495)
    assert book.process_existing_order(order, first_slice) is third


def test_pending_slice_cannot_be_repriced_at_same_sequence() -> None:
    order = _approved()
    book = ReplayBook(PortfolioLedger(Decimal(1000)))
    empty = TimeSlice(1, NOW, ())
    first = book.process_existing_order(order, empty)
    assert first.state is OrderState.ACTIVE
    assert book.process_existing_order(order, TimeSlice(1, NOW, ())) is first
    with pytest.raises(TradingResearchError, match="conflicting same-slice"):
        book.process_existing_order(order, TimeSlice(1, NOW, (_quote(NOW),)))
    assert book.ledger.cash == Decimal(1000)
