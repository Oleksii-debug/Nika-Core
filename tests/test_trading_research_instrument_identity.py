from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.trading_research.accounting import AccountSnapshot, PortfolioLedger, Position
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
    OrderIntent,
    OrderType,
    RiskApprovedOrder,
    Side,
    SimulatedFill,
)
from nika_core.trading_research.persistence import TradingStateRepository
from nika_core.trading_research.replay import SimulationExecutionEngine, TimeSlice
from nika_core.trading_research.risk import RiskEngine, RiskLimits, RiskState

_NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
_VENUE_A = Venue("SIM-A", "UTC")
_VENUE_B = Venue("SIM-B", "Europe/Bratislava")
_INSTRUMENT_A = Instrument("SAME", _VENUE_A, "USD")
_INSTRUMENT_B = Instrument("SAME", _VENUE_B, "USD")


def _quote(
    instrument: Instrument,
    *,
    bid: str = "99",
    ask: str = "100",
    size: str = "10",
) -> Quote:
    return Quote(
        instrument,
        EventTime(_NOW, _NOW, _NOW),
        Decimal(bid),
        Decimal(ask),
        Decimal(size),
        Decimal(size),
    )


def _approved(
    instrument: Instrument,
    *,
    side: Side = Side.BUY,
    order_type: OrderType = OrderType.MARKET,
    limit_price: Decimal | None = None,
    policy: ExecutionPolicy | None = None,
) -> RiskApprovedOrder:
    intent = OrderIntent(
        intent_id="shared-intent",
        instrument=instrument,
        side=side,
        order_type=order_type,
        quantity=Decimal(1),
        submitted_at=_NOW,
        submitted_slice=0,
        limit_price=limit_price,
    )
    return RiskApprovedOrder(
        "shared-approval",
        intent,
        _NOW,
        0,
        ExecutionPolicy("identity") if policy is None else policy,
    )


def _fill(instrument: Instrument, fill_id: str) -> SimulatedFill:
    return SimulatedFill(
        fill_id=fill_id,
        approval_id="approval",
        intent_id="intent",
        instrument=instrument,
        side=Side.BUY,
        quantity=Decimal(1),
        price=Decimal(100),
        fee=Decimal(0),
        filled_at=_NOW,
        filled_slice=1,
    )


def test_portfolio_keeps_equal_native_ids_on_distinct_venues_separate() -> None:
    ledger = PortfolioLedger(Decimal(1000))
    ledger.apply_fill(_fill(_INSTRUMENT_A, "fill-a"))
    ledger.apply_fill(_fill(_INSTRUMENT_B, "fill-b"))

    snapshot = ledger.snapshot(
        {
            instrument_identity(_INSTRUMENT_A): Decimal(100),
            instrument_identity(_INSTRUMENT_B): Decimal(200),
        }
    )

    assert len(snapshot.positions) == 2
    assert ledger.position(_INSTRUMENT_A).quantity == Decimal(1)
    assert ledger.position(_INSTRUMENT_B).quantity == Decimal(1)
    assert snapshot.gross_exposure == Decimal(300)
    assert snapshot.net_exposure == Decimal(300)


def test_symbol_only_mark_cannot_cross_the_full_identity_boundary() -> None:
    ledger = PortfolioLedger(Decimal(1000))
    ledger.apply_fill(_fill(_INSTRUMENT_A, "fill-a"))

    with pytest.raises(TradingResearchError, match="positive mark required"):
        ledger.snapshot({"SAME": Decimal(100)})


def test_replay_never_executes_against_other_venue_market_data() -> None:
    order = _approved(_INSTRUMENT_B)
    engine = SimulationExecutionEngine()

    wrong_venue = engine.execute(order, TimeSlice(1, _NOW, (_quote(_INSTRUMENT_A),)))
    assert wrong_venue.fill is None
    assert wrong_venue.reason == "no executable market data"

    correct_venue = engine.execute(
        order,
        TimeSlice(
            1,
            _NOW,
            (
                _quote(_INSTRUMENT_A, ask="50"),
                _quote(_INSTRUMENT_B, ask="101"),
            ),
        ),
    )
    assert correct_venue.fill is not None
    assert correct_venue.fill.instrument == _INSTRUMENT_B
    assert correct_venue.fill.price == Decimal(101)


def test_deterministic_fill_id_binds_full_instrument_identity() -> None:
    engine = SimulationExecutionEngine()
    fill_a = engine.execute(
        _approved(_INSTRUMENT_A),
        TimeSlice(1, _NOW, (_quote(_INSTRUMENT_A),)),
    ).fill
    fill_b = engine.execute(
        _approved(_INSTRUMENT_B),
        TimeSlice(1, _NOW, (_quote(_INSTRUMENT_B),)),
    ).fill

    assert fill_a is not None
    assert fill_b is not None
    assert fill_a.fill_id != fill_b.fill_id


def test_risk_position_limit_does_not_merge_equal_ids_from_other_venue() -> None:
    snapshot = AccountSnapshot(
        cash=Decimal(900),
        fees=Decimal(0),
        realized_pnl=Decimal(0),
        unrealized_pnl=Decimal(0),
        equity=Decimal(1000),
        gross_exposure=Decimal(100),
        net_exposure=Decimal(100),
        positions=(Position(_INSTRUMENT_A, Decimal(1), Decimal(100), Decimal(0)),),
    )
    limits = RiskLimits(
        max_abs_position=Decimal(1),
        max_gross_exposure=Decimal(1000),
        max_net_exposure=Decimal(1000),
        max_session_loss=Decimal(1000),
        max_drawdown=Decimal(1000),
        max_leverage=Decimal(10),
    )
    intent = OrderIntent(
        "venue-b",
        _INSTRUMENT_B,
        Side.BUY,
        OrderType.MARKET,
        Decimal(1),
        _NOW,
        0,
    )

    approved = RiskEngine(limits).approve(
        intent,
        snapshot=snapshot,
        mark_price=Decimal(100),
        pending_signed_quantity=Decimal(0),
        approved_at=_NOW,
        approved_slice=0,
        policy=ExecutionPolicy("risk"),
        risk_state=RiskState(Decimal(1000), Decimal(1000)),
    )

    assert approved.intent.instrument == _INSTRUMENT_B


def test_persistence_records_complete_instrument_identity(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repo = TradingStateRepository(store)
    repo.initialize()

    fill = _fill(_INSTRUMENT_B, "fill-persist")
    ledger = PortfolioLedger(Decimal(1000))
    ledger.apply_fill(fill)
    snapshot = ledger.snapshot({instrument_identity(_INSTRUMENT_B): Decimal(100)})
    assert repo.commit_fill_and_account(fill, snapshot)

    with store.connection() as conn:
        row = conn.execute(
            "SELECT venue_id, venue_timezone, instrument_id, currency "
            "FROM trading_research_fills WHERE fill_id = ?",
            (fill.fill_id,),
        ).fetchone()
    assert tuple(row) == ("SIM-B", "Europe/Bratislava", "SAME", "USD")

    payload = repo.account_payload()
    assert payload is not None
    positions = payload["positions"]
    assert isinstance(positions, list)
    assert positions[0]["venue_id"] == "SIM-B"
    assert positions[0]["venue_timezone"] == "Europe/Bratislava"
    assert positions[0]["instrument_id"] == "SAME"
    assert positions[0]["currency"] == "USD"


def test_nonempty_v1_state_fails_closed_instead_of_inventing_venue(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    _install_v1_schema(store, with_rows=True)

    with pytest.raises(RuntimeError, match="lacks venue identity"):
        TradingStateRepository(store).initialize()


def test_empty_v1_state_upgrades_additively_to_identity_schema(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    _install_v1_schema(store, with_rows=False)

    repo = TradingStateRepository(store)
    repo.initialize()

    with store.connection() as conn:
        rows = conn.execute("PRAGMA table_info(trading_research_fills)").fetchall()
    columns = {str(row["name"]) for row in rows}
    assert {"venue_id", "venue_timezone", "instrument_id", "currency"} <= columns


@pytest.mark.parametrize(
    ("side", "bid", "ask"),
    (
        (Side.BUY, "98", "99"),
        (Side.SELL, "101", "102"),
    ),
)
def test_adverse_slippage_never_crosses_limit_price(
    side: Side,
    bid: str,
    ask: str,
) -> None:
    order = _approved(
        _INSTRUMENT_A,
        side=side,
        order_type=OrderType.LIMIT,
        limit_price=Decimal(100),
        policy=ExecutionPolicy("limit-slippage", slippage_bps=Decimal(500)),
    )

    update = SimulationExecutionEngine().execute(
        order,
        TimeSlice(1, _NOW, (_quote(_INSTRUMENT_A, bid=bid, ask=ask),)),
    )

    assert update.fill is not None
    assert update.fill.price == Decimal(100)


def _install_v1_schema(store: SQLiteStore, *, with_rows: bool) -> None:
    with store.connection() as conn:
        conn.execute(
            "CREATE TABLE trading_research_schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "CREATE TABLE trading_research_fills ("
            "fill_id TEXT PRIMARY KEY, approval_id TEXT NOT NULL, intent_id TEXT NOT NULL, "
            "instrument_id TEXT NOT NULL, side TEXT NOT NULL, quantity TEXT NOT NULL, "
            "price TEXT NOT NULL, fee TEXT NOT NULL, filled_at TEXT NOT NULL, "
            "filled_slice INTEGER NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE trading_research_account_state ("
            "singleton INTEGER PRIMARY KEY CHECK(singleton = 1), payload TEXT NOT NULL, "
            "last_fill_id TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO trading_research_schema_migrations(version) VALUES (1)"
        )
        if not with_rows:
            return
        conn.execute(
            "INSERT INTO trading_research_fills("
            "fill_id, approval_id, intent_id, instrument_id, side, quantity, price, fee, "
            "filled_at, filled_slice) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-fill",
                "approval",
                "intent",
                "SAME",
                "buy",
                "1",
                "100",
                "0",
                _NOW.isoformat(),
                1,
            ),
        )
        conn.execute(
            "INSERT INTO trading_research_account_state(singleton, payload, last_fill_id) "
            "VALUES (1, '{}', 'legacy-fill')"
        )
