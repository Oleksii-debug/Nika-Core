from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.trading_research.accounting import AccountSnapshot, PortfolioLedger, Position
from nika_core.trading_research.contracts import (
    Bar,
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
    OrderType,
    RiskApprovedOrder,
    Side,
    SimulatedFill,
)
from nika_core.trading_research.persistence import TradingStateRepository
from nika_core.trading_research.replay import (
    ReplayBook,
    SimulationExecutionEngine,
    TimeSlice,
)
from nika_core.trading_research.risk import (
    PendingRiskOrder,
    RiskEngine,
    RiskLimits,
    RiskState,
)

_NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
_VENUE_A = Venue("SIM-A", "UTC")
_VENUE_B = Venue("SIM-B", "Europe/Bratislava")
_INSTRUMENT_A = Instrument("SAME", _VENUE_A, "USD")
_INSTRUMENT_B = Instrument("SAME", _VENUE_B, "USD")


def _authority(
    order_id: str = "shared-order",
    *,
    workspace_id: str = "workspace",
    run_id: str = "run",
    submitted_slice: int = 0,
    submitted_at: datetime = _NOW,
) -> OrderAuthority:
    return OrderAuthority(
        workspace_id,
        run_id,
        order_id,
        submitted_at,
        submitted_slice,
    )


def _quote(
    instrument: Instrument,
    *,
    bid: str = "99",
    ask: str = "100",
    size: str = "10",
    source_sequence: int = 0,
) -> Quote:
    return Quote(
        instrument,
        EventTime(_NOW, _NOW, _NOW),
        Decimal(bid),
        Decimal(ask),
        Decimal(size),
        Decimal(size),
        source_sequence,
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
        _authority(),
        _NOW,
        0,
        ExecutionPolicy("identity") if policy is None else policy,
    )


def _fill(
    instrument: Instrument,
    fill_id: str,
    *,
    workspace_id: str = "workspace",
    run_id: str = "run",
) -> SimulatedFill:
    return SimulatedFill(
        fill_id=fill_id,
        approval_id="approval",
        intent_id="intent",
        authority=_authority(fill_id, workspace_id=workspace_id, run_id=run_id),
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


def test_portfolio_ledger_rejects_cross_run_account_mixing() -> None:
    ledger = PortfolioLedger(Decimal(1000))
    ledger.apply_fill(_fill(_INSTRUMENT_A, "fill-a", run_id="run-a"))

    with pytest.raises(TradingResearchError, match="cannot mix workspace/run"):
        ledger.apply_fill(_fill(_INSTRUMENT_A, "fill-b", run_id="run-b"))


def test_portfolio_ledger_exact_retry_is_idempotent_but_conflict_fails() -> None:
    ledger = PortfolioLedger(Decimal(1000))
    fill = _fill(_INSTRUMENT_A, "fill-a")
    ledger.apply_fill(fill)
    ledger.apply_fill(fill)
    assert ledger.cash == Decimal(900)

    with pytest.raises(TradingResearchError, match="conflicting in-memory fill identity"):
        ledger.apply_fill(replace(fill, price=Decimal(101)))


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
                _quote(_INSTRUMENT_A, bid="49", ask="50"),
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
        authority=_authority("venue-b"),
        snapshot=snapshot,
        mark_price=Decimal(100),
        pending_signed_quantity=Decimal(0),
        approved_at=_NOW,
        approved_slice=0,
        policy=ExecutionPolicy("risk"),
        risk_state=RiskState(Decimal(1000), Decimal(1000)),
    )

    assert approved.intent.instrument == _INSTRUMENT_B


def test_risk_generated_approval_id_binds_full_instrument_identity() -> None:
    snapshot = AccountSnapshot(
        cash=Decimal(1000),
        fees=Decimal(0),
        realized_pnl=Decimal(0),
        unrealized_pnl=Decimal(0),
        equity=Decimal(1000),
        gross_exposure=Decimal(0),
        net_exposure=Decimal(0),
        positions=(),
    )
    limits = RiskLimits(
        max_abs_position=Decimal(10),
        max_gross_exposure=Decimal(1000),
        max_net_exposure=Decimal(1000),
        max_session_loss=Decimal(1000),
        max_drawdown=Decimal(1000),
        max_leverage=Decimal(10),
    )
    engine = RiskEngine(limits)

    def approve(instrument: Instrument) -> RiskApprovedOrder:
        return engine.approve(
            OrderIntent(
                "same-intent",
                instrument,
                Side.BUY,
                OrderType.MARKET,
                Decimal(1),
                _NOW,
                0,
            ),
            authority=_authority("same-order"),
            snapshot=snapshot,
            mark_price=Decimal(100),
            pending_signed_quantity=Decimal(0),
            approved_at=_NOW,
            approved_slice=0,
            policy=ExecutionPolicy("approval-identity"),
            risk_state=RiskState(Decimal(1000), Decimal(1000)),
        )

    assert approve(_INSTRUMENT_A).approval_id != approve(_INSTRUMENT_B).approval_id



def test_strategy_intent_id_and_time_do_not_define_risk_approval_identity() -> None:
    snapshot = AccountSnapshot(
        cash=Decimal(1000),
        fees=Decimal(0),
        realized_pnl=Decimal(0),
        unrealized_pnl=Decimal(0),
        equity=Decimal(1000),
        gross_exposure=Decimal(0),
        net_exposure=Decimal(0),
        positions=(),
    )
    limits = RiskLimits(
        max_abs_position=Decimal(10),
        max_gross_exposure=Decimal(1000),
        max_net_exposure=Decimal(1000),
        max_session_loss=Decimal(1000),
        max_drawdown=Decimal(1000),
        max_leverage=Decimal(10),
    )
    engine = RiskEngine(limits)
    authority = _authority("host-stamped-order")

    def approve(intent_id: str, proposed_at: datetime) -> RiskApprovedOrder:
        return engine.approve(
            OrderIntent(
                intent_id,
                _INSTRUMENT_A,
                Side.BUY,
                OrderType.MARKET,
                Decimal(1),
                proposed_at,
                99,
            ),
            authority=authority,
            snapshot=snapshot,
            mark_price=Decimal(100),
            pending_signed_quantity=Decimal(0),
            approved_at=_NOW,
            approved_slice=0,
            policy=ExecutionPolicy("host-stamp"),
            risk_state=RiskState(Decimal(1000), Decimal(1000)),
        )

    first = approve("strategy-a", _NOW + timedelta(days=1))
    second = approve("strategy-b", _NOW + timedelta(days=2))
    assert first.approval_id == second.approval_id
    assert first.authority == authority
    assert second.authority == authority


def test_replay_book_state_does_not_alias_shared_approval_id_across_venues() -> None:
    book = ReplayBook(PortfolioLedger(Decimal(1000)))
    first = book.process_existing_order(
        _approved(_INSTRUMENT_A),
        TimeSlice(1, _NOW, (_quote(_INSTRUMENT_A),)),
    )
    second = book.process_existing_order(
        _approved(_INSTRUMENT_B),
        TimeSlice(1, _NOW, (_quote(_INSTRUMENT_B),)),
    )

    assert first.fill is not None
    assert second.fill is not None
    assert first.fill.instrument == _INSTRUMENT_A
    assert second.fill.instrument == _INSTRUMENT_B
    assert first.fill.fill_id != second.fill.fill_id
    assert len(book.ledger.snapshot(
        {
            instrument_identity(_INSTRUMENT_A): Decimal(100),
            instrument_identity(_INSTRUMENT_B): Decimal(100),
        }
    ).positions) == 2


@pytest.mark.parametrize("bad_slice", (True, 1.5))
def test_order_and_replay_chronology_reject_non_integer_slices(bad_slice: object) -> None:
    with pytest.raises(TradingResearchError, match="submitted_slice must be a non-negative integer"):
        OrderIntent(
            "bad-strategy-slice",
            _INSTRUMENT_A,
            Side.BUY,
            OrderType.MARKET,
            Decimal(1),
            _NOW,
            bad_slice,
        )

    intent = OrderIntent(
        "valid-intent",
        _INSTRUMENT_A,
        Side.BUY,
        OrderType.MARKET,
        Decimal(1),
        _NOW,
        0,
    )
    authority = _authority("integer-chronology")
    policy = ExecutionPolicy("integer-chronology")

    with pytest.raises(TradingResearchError, match="approved_slice must be a non-negative integer"):
        RiskApprovedOrder(
            "bad-approval-slice",
            intent,
            authority,
            _NOW,
            bad_slice,
            policy,
        )

    with pytest.raises(TradingResearchError, match="filled_slice must be an integer"):
        SimulatedFill(
            fill_id="bad-fill-slice",
            approval_id="approval",
            intent_id=intent.intent_id,
            authority=authority,
            instrument=_INSTRUMENT_A,
            side=Side.BUY,
            quantity=Decimal(1),
            price=Decimal(100),
            fee=Decimal(0),
            filled_at=_NOW,
            filled_slice=bad_slice,
        )

    with pytest.raises(TradingResearchError, match="slice index must be a non-negative integer"):
        TimeSlice(bad_slice, _NOW, (_quote(_INSTRUMENT_A),))


def test_execution_uses_host_submission_not_strategy_proposal_metadata() -> None:
    intent = OrderIntent(
        "strategy-controlled-id",
        _INSTRUMENT_A,
        Side.BUY,
        OrderType.MARKET,
        Decimal(1),
        _NOW + timedelta(days=1),
        99,
    )
    order = RiskApprovedOrder(
        "host-approval",
        intent,
        _authority("host-order", submitted_slice=0),
        _NOW,
        0,
        ExecutionPolicy("host-authority"),
    )

    update = SimulationExecutionEngine().execute(
        order,
        TimeSlice(1, _NOW, (_quote(_INSTRUMENT_A),)),
    )

    assert update.fill is not None
    assert update.fill.authority.order_id == "host-order"


def test_pending_risk_from_other_run_fails_closed() -> None:
    snapshot = AccountSnapshot(
        cash=Decimal(1000),
        fees=Decimal(0),
        realized_pnl=Decimal(0),
        unrealized_pnl=Decimal(0),
        equity=Decimal(1000),
        gross_exposure=Decimal(0),
        net_exposure=Decimal(0),
        positions=(),
    )
    limits = RiskLimits(
        max_abs_position=Decimal(10),
        max_gross_exposure=Decimal(1000),
        max_net_exposure=Decimal(1000),
        max_session_loss=Decimal(1000),
        max_drawdown=Decimal(1000),
        max_leverage=Decimal(10),
    )
    engine = RiskEngine(limits)
    pending_intent = OrderIntent(
        "pending",
        _INSTRUMENT_A,
        Side.BUY,
        OrderType.MARKET,
        Decimal(1),
        _NOW,
        0,
    )
    pending = RiskApprovedOrder(
        "pending-approval",
        pending_intent,
        _authority("pending", run_id="run-a"),
        _NOW,
        0,
        ExecutionPolicy("pending"),
    )
    candidate = OrderIntent(
        "candidate",
        _INSTRUMENT_A,
        Side.BUY,
        OrderType.MARKET,
        Decimal(1),
        _NOW,
        0,
    )

    with pytest.raises(TradingResearchError, match="another workspace/run"):
        engine.approve(
            candidate,
            authority=_authority("candidate", run_id="run-b"),
            snapshot=snapshot,
            mark_price=Decimal(100),
            pending_signed_quantity=Decimal(0),
            approved_at=_NOW,
            approved_slice=0,
            policy=ExecutionPolicy("candidate"),
            risk_state=RiskState(Decimal(1000), Decimal(1000)),
            pending_orders=(PendingRiskOrder(pending, Decimal(100)),),
        )


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
            "SELECT workspace_id, run_id, order_id, venue_id, venue_timezone, "
            "instrument_id, currency FROM trading_research_run_fills "
            "WHERE workspace_id = ? AND run_id = ? AND fill_id = ?",
            ("workspace", "run", fill.fill_id),
        ).fetchone()
    assert tuple(row) == (
        "workspace",
        "run",
        "fill-persist",
        "SIM-B",
        "Europe/Bratislava",
        "SAME",
        "USD",
    )

    payload = repo.account_payload("workspace", "run")
    assert payload is not None
    positions = payload["positions"]
    assert isinstance(positions, list)
    assert positions[0]["venue_id"] == "SIM-B"
    assert positions[0]["venue_timezone"] == "Europe/Bratislava"
    assert positions[0]["instrument_id"] == "SAME"
    assert positions[0]["currency"] == "USD"


def test_persistence_isolates_equal_fill_ids_and_accounts_between_runs(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repo = TradingStateRepository(store)
    repo.initialize()

    fill_a = _fill(_INSTRUMENT_A, "same-fill", run_id="run-a")
    fill_b = _fill(_INSTRUMENT_B, "same-fill", run_id="run-b")

    ledger_a = PortfolioLedger(Decimal(1000))
    ledger_a.apply_fill(fill_a)
    snapshot_a = ledger_a.snapshot({instrument_identity(_INSTRUMENT_A): Decimal(100)})

    ledger_b = PortfolioLedger(Decimal(2000))
    ledger_b.apply_fill(fill_b)
    snapshot_b = ledger_b.snapshot({instrument_identity(_INSTRUMENT_B): Decimal(100)})

    assert repo.commit_fill_and_account(fill_a, snapshot_a)
    assert repo.commit_fill_and_account(fill_b, snapshot_b)

    assert repo.fill_count("workspace", "run-a") == 1
    assert repo.fill_count("workspace", "run-b") == 1
    assert repo.has_fill("workspace", "run-a", "same-fill")
    assert repo.has_fill("workspace", "run-b", "same-fill")
    assert repo.account_payload("workspace", "run-a")["cash"] == "900"
    assert repo.account_payload("workspace", "run-b")["cash"] == "1900"



def test_conflicting_retry_of_same_scoped_fill_id_fails_closed(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    repo = TradingStateRepository(store)
    repo.initialize()

    fill = _fill(_INSTRUMENT_A, "conflict-fill")
    ledger = PortfolioLedger(Decimal(1000))
    ledger.apply_fill(fill)
    snapshot = ledger.snapshot({instrument_identity(_INSTRUMENT_A): Decimal(100)})
    assert repo.commit_fill_and_account(fill, snapshot)

    conflicting = replace(fill, price=Decimal(101))
    with pytest.raises(RuntimeError, match="conflicting durable fill identity"):
        repo.commit_fill_and_account(conflicting, snapshot)

    assert repo.fill_count("workspace", "run") == 1
    assert repo.commit_fill_and_account(fill, snapshot) is False


def test_unversioned_legacy_rows_fail_closed(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    with store.connection() as conn:
        conn.execute(
            "CREATE TABLE trading_research_fills ("
            "fill_id TEXT PRIMARY KEY, approval_id TEXT NOT NULL, intent_id TEXT NOT NULL, "
            "instrument_id TEXT NOT NULL, side TEXT NOT NULL, quantity TEXT NOT NULL, "
            "price TEXT NOT NULL, fee TEXT NOT NULL, filled_at TEXT NOT NULL, "
            "filled_slice INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO trading_research_fills("
            "fill_id, approval_id, intent_id, instrument_id, side, quantity, price, fee, "
            "filled_at, filled_slice) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "orphan-fill",
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

    with pytest.raises(RuntimeError, match="unversioned trading state"):
        TradingStateRepository(store).initialize()


def test_nonempty_v1_state_fails_closed_instead_of_inventing_venue(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    _install_v1_schema(store, with_rows=True)

    with pytest.raises(RuntimeError, match="lacks venue/run identity"):
        TradingStateRepository(store).initialize()


def test_empty_v1_state_upgrades_to_run_scoped_v3_schema(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    _install_v1_schema(store, with_rows=False)

    repo = TradingStateRepository(store)
    repo.initialize()

    with store.connection() as conn:
        fill_rows = conn.execute(
            "PRAGMA table_info(trading_research_run_fills)"
        ).fetchall()
        account_rows = conn.execute(
            "PRAGMA table_info(trading_research_run_account_state)"
        ).fetchall()
    fill_columns = {str(row["name"]) for row in fill_rows}
    account_columns = {str(row["name"]) for row in account_rows}
    assert {
        "workspace_id",
        "run_id",
        "order_id",
        "venue_id",
        "venue_timezone",
        "instrument_id",
        "currency",
    } <= fill_columns
    assert account_columns == {"workspace_id", "run_id", "payload", "last_fill_id"}


def test_bar_without_interval_authority_is_not_executable_market_data() -> None:
    order = _approved(_INSTRUMENT_A)
    bar = Bar(
        _INSTRUMENT_A,
        EventTime(_NOW, _NOW, _NOW),
        Decimal(99),
        Decimal(101),
        Decimal(98),
        Decimal(100),
        Decimal(10),
        1,
    )

    update = SimulationExecutionEngine().execute(
        order,
        TimeSlice(1, _NOW, (bar,)),
    )

    assert update.fill is None
    assert update.reason == "no executable market data"


def test_non_executable_bar_does_not_hide_authoritative_quote() -> None:
    order = _approved(_INSTRUMENT_A)
    quote = _quote(_INSTRUMENT_A, ask="101", source_sequence=1)
    bar = Bar(
        _INSTRUMENT_A,
        EventTime(_NOW, _NOW, _NOW),
        Decimal(90),
        Decimal(200),
        Decimal(80),
        Decimal(150),
        Decimal(10),
        2,
    )

    update = SimulationExecutionEngine().execute(
        order,
        TimeSlice(1, _NOW, (quote, bar)),
    )

    assert update.fill is not None
    assert update.fill.price == Decimal(101)


def test_conflicting_equal_sequence_events_fail_closed() -> None:
    first = _quote(_INSTRUMENT_A, ask="100", source_sequence=7)
    second = _quote(_INSTRUMENT_A, ask="101", source_sequence=7)

    with pytest.raises(TradingResearchError, match="ambiguous same-slice market chronology"):
        TimeSlice(1, _NOW, (first, second))


def test_source_sequence_is_explicit_same_timestamp_order_authority() -> None:
    order = _approved(_INSTRUMENT_A)
    earlier = _quote(_INSTRUMENT_A, ask="100", source_sequence=1)
    later = _quote(_INSTRUMENT_A, ask="101", source_sequence=2)

    update = SimulationExecutionEngine().execute(
        order,
        TimeSlice(1, _NOW, (later, earlier)),
    )

    assert update.fill is not None
    assert update.fill.price == Decimal(101)


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


def test_migration_schema_with_text_version_fails_closed(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    with store.connection() as conn:
        conn.execute(
            "CREATE TABLE trading_research_schema_migrations ("
            "version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO trading_research_schema_migrations(version) VALUES ('3')"
        )

    with pytest.raises(RuntimeError, match="migration column types"):
        TradingStateRepository(store).initialize()


def test_v3_schema_with_global_fill_primary_key_fails_closed(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    with store.connection() as conn:
        conn.execute(
            "CREATE TABLE trading_research_schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO trading_research_schema_migrations(version) VALUES (3)"
        )
        conn.execute(
            "CREATE TABLE trading_research_run_fills ("
            "workspace_id TEXT NOT NULL, run_id TEXT NOT NULL, "
            "fill_id TEXT NOT NULL PRIMARY KEY, approval_id TEXT NOT NULL, "
            "intent_id TEXT NOT NULL, order_id TEXT NOT NULL, venue_id TEXT NOT NULL, "
            "venue_timezone TEXT NOT NULL, instrument_id TEXT NOT NULL, "
            "currency TEXT NOT NULL, side TEXT NOT NULL, quantity TEXT NOT NULL, "
            "price TEXT NOT NULL, fee TEXT NOT NULL, filled_at TEXT NOT NULL, "
            "filled_slice INTEGER NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE trading_research_run_account_state ("
            "workspace_id TEXT NOT NULL, run_id TEXT NOT NULL, payload TEXT NOT NULL, "
            "last_fill_id TEXT NOT NULL, PRIMARY KEY(workspace_id, run_id))"
        )

    with pytest.raises(RuntimeError, match="run fill primary key"):
        TradingStateRepository(store).initialize()


def test_v3_schema_with_text_fill_slice_fails_closed(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    with store.connection() as conn:
        conn.execute(
            "CREATE TABLE trading_research_schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO trading_research_schema_migrations(version) VALUES (3)"
        )
        conn.execute(
            "CREATE TABLE trading_research_run_fills ("
            "workspace_id TEXT NOT NULL, run_id TEXT NOT NULL, fill_id TEXT NOT NULL, "
            "approval_id TEXT NOT NULL, intent_id TEXT NOT NULL, order_id TEXT NOT NULL, "
            "venue_id TEXT NOT NULL, venue_timezone TEXT NOT NULL, instrument_id TEXT NOT NULL, "
            "currency TEXT NOT NULL, side TEXT NOT NULL, quantity TEXT NOT NULL, "
            "price TEXT NOT NULL, fee TEXT NOT NULL, filled_at TEXT NOT NULL, "
            "filled_slice TEXT NOT NULL, PRIMARY KEY(workspace_id, run_id, fill_id))"
        )
        conn.execute(
            "CREATE TABLE trading_research_run_account_state ("
            "workspace_id TEXT NOT NULL, run_id TEXT NOT NULL, payload TEXT NOT NULL, "
            "last_fill_id TEXT NOT NULL, PRIMARY KEY(workspace_id, run_id))"
        )

    with pytest.raises(RuntimeError, match="run fill column types"):
        TradingStateRepository(store).initialize()


def test_v3_schema_with_integer_account_payload_fails_closed(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    with store.connection() as conn:
        conn.execute(
            "CREATE TABLE trading_research_schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "INSERT INTO trading_research_schema_migrations(version) VALUES (3)"
        )
        conn.execute(
            "CREATE TABLE trading_research_run_fills ("
            "workspace_id TEXT NOT NULL, run_id TEXT NOT NULL, fill_id TEXT NOT NULL, "
            "approval_id TEXT NOT NULL, intent_id TEXT NOT NULL, order_id TEXT NOT NULL, "
            "venue_id TEXT NOT NULL, venue_timezone TEXT NOT NULL, instrument_id TEXT NOT NULL, "
            "currency TEXT NOT NULL, side TEXT NOT NULL, quantity TEXT NOT NULL, "
            "price TEXT NOT NULL, fee TEXT NOT NULL, filled_at TEXT NOT NULL, "
            "filled_slice INTEGER NOT NULL, PRIMARY KEY(workspace_id, run_id, fill_id))"
        )
        conn.execute(
            "CREATE TABLE trading_research_run_account_state ("
            "workspace_id TEXT NOT NULL, run_id TEXT NOT NULL, payload INTEGER NOT NULL, "
            "last_fill_id TEXT NOT NULL, PRIMARY KEY(workspace_id, run_id))"
        )

    with pytest.raises(RuntimeError, match="run account column types"):
        TradingStateRepository(store).initialize()


def test_nonempty_v2_state_fails_closed_without_run_scope(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    _install_v2_schema(store, with_rows=True)

    with pytest.raises(RuntimeError, match="lacks workspace/run identity"):
        TradingStateRepository(store).initialize()

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


def _install_v2_schema(store: SQLiteStore, *, with_rows: bool) -> None:
    with store.connection() as conn:
        conn.execute(
            "CREATE TABLE trading_research_schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        conn.execute(
            "CREATE TABLE trading_research_fills ("
            "fill_id TEXT PRIMARY KEY, approval_id TEXT NOT NULL, intent_id TEXT NOT NULL, "
            "venue_id TEXT NOT NULL, venue_timezone TEXT NOT NULL, instrument_id TEXT NOT NULL, "
            "currency TEXT NOT NULL, side TEXT NOT NULL, quantity TEXT NOT NULL, "
            "price TEXT NOT NULL, fee TEXT NOT NULL, filled_at TEXT NOT NULL, "
            "filled_slice INTEGER NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE trading_research_account_state ("
            "singleton INTEGER PRIMARY KEY CHECK(singleton = 1), payload TEXT NOT NULL, "
            "last_fill_id TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO trading_research_schema_migrations(version) VALUES (2)"
        )
        if not with_rows:
            return
        conn.execute(
            "INSERT INTO trading_research_fills("
            "fill_id, approval_id, intent_id, venue_id, venue_timezone, instrument_id, "
            "currency, side, quantity, price, fee, filled_at, filled_slice) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-v2-fill",
                "approval",
                "intent",
                "SIM",
                "UTC",
                "SAME",
                "USD",
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
            "VALUES (1, '{}', 'legacy-v2-fill')"
        )
