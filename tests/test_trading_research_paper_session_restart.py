from __future__ import annotations

import ast
import inspect
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.trading_research import paper_session as session_module
from nika_core.trading_research.contracts import (
    EventTime,
    Instrument,
    Provenance,
    Quote,
    Venue,
)
from nika_core.trading_research.dataset import DatasetVersion
from nika_core.trading_research.orders import (
    ExecutionPolicy,
    OrderIntent,
    OrderState,
    OrderType,
    Side,
)
from nika_core.trading_research.paper_session import (
    PaperDataProvenance,
    PaperSessionConfig,
    PaperSessionConflict,
    PaperSessionIntegrityError,
    PaperTradingSession,
)
from nika_core.trading_research.replay import TimeSlice
from nika_core.trading_research.risk import RiskLimits, RiskRejected

NOW = datetime(2026, 1, 1, tzinfo=UTC)
INSTRUMENT = Instrument("TEST", Venue("SIM", "UTC"), "USD")
DATA_VERSION = DatasetVersion(
    dataset_id="paper-fixture",
    version="v1",
    raw_hash="a" * 64,
    semantic_hash="b" * 64,
    provenance=Provenance("fixture-source", acquired_at=NOW, license_id="TEST-ONLY"),
)


def _quote(at: datetime, *, size: str = "10") -> Quote:
    return Quote(
        INSTRUMENT,
        EventTime(at, at, at),
        Decimal(99),
        Decimal(101),
        Decimal(size),
        Decimal(size),
    )


def _data(*, cutoff: datetime | None = None, semantic_hash: str | None = None):
    version = DATA_VERSION
    if semantic_hash is not None:
        version = DatasetVersion(
            dataset_id=version.dataset_id,
            version=version.version,
            raw_hash=version.raw_hash,
            semantic_hash=semantic_hash,
            provenance=version.provenance,
        )
    return PaperDataProvenance.from_dataset_version(
        version,
        cutoff_at=cutoff or NOW + timedelta(days=1),
    )


def _limits(*, max_position: str = "20", allow_short: bool = True) -> RiskLimits:
    return RiskLimits(
        max_abs_position=Decimal(max_position),
        max_gross_exposure=Decimal(100_000),
        max_net_exposure=Decimal(100_000),
        max_session_loss=Decimal(100_000),
        max_drawdown=Decimal(100_000),
        allow_short=allow_short,
        max_leverage=Decimal(100),
    )


def _config(
    session_id: str,
    *,
    policy: ExecutionPolicy | None = None,
    limits: RiskLimits | None = None,
    data: PaperDataProvenance | None = None,
) -> PaperSessionConfig:
    return PaperSessionConfig(
        session_id=session_id,
        strategy_id="strategy-a",
        strategy_version="1",
        starting_cash=Decimal(1000),
        data=data or _data(),
        execution_policy=policy or ExecutionPolicy("paper-v1"),
        risk_limits=limits or _limits(),
    )


def _intent(
    intent_id: str,
    *,
    at: datetime,
    slice_index: int,
    quantity: str = "1",
    side: Side = Side.BUY,
    expires_at: datetime | None = None,
) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        instrument=INSTRUMENT,
        side=side,
        order_type=OrderType.MARKET,
        quantity=Decimal(quantity),
        submitted_at=at,
        submitted_slice=slice_index,
        expires_at=expires_at,
    )


def test_session_identity_config_and_data_binding_survive_restart(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    config = _config("session-identity")
    session = PaperTradingSession.start(store, config)
    session.process_slice(TimeSlice(0, NOW, (_quote(NOW),)))

    restarted = PaperTradingSession.resume(
        SQLiteStore(tmp_path / "nika.db"),
        config.session_id,
        expected_data=config.data,
    )
    assert restarted.snapshot.config == config
    assert restarted.snapshot.cursor_slice == 0
    assert restarted.snapshot.cursor_at == NOW
    assert restarted.snapshot.account.cash == Decimal(1000)
    assert restarted.snapshot.risk_state.session_start_equity == Decimal(1000)


def test_crash_before_activation_restores_same_pending_risk_approved_order(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    policy = ExecutionPolicy("latency", latency=timedelta(minutes=5))
    session = PaperTradingSession.start(store, _config("before-activation", policy=policy))
    session.process_slice(TimeSlice(0, NOW, (_quote(NOW),)))
    approved = session.queue_intent(
        _intent("latency-order", at=NOW, slice_index=0, quantity="2"),
        mark_price=Decimal(100),
    )

    restarted = PaperTradingSession.resume(SQLiteStore(tmp_path / "nika.db"), "before-activation")
    durable = restarted.snapshot.open_orders[0]
    assert durable.order == approved
    assert durable.state is OrderState.PENDING
    assert durable.remaining_quantity == Decimal(2)

    one_minute = NOW + timedelta(minutes=1)
    pending = restarted.process_slice(TimeSlice(1, one_minute, (_quote(one_minute),)))
    assert pending.updates[0].state is OrderState.PENDING
    assert restarted.fill_count == 0

    active_at = NOW + timedelta(minutes=5)
    filled = restarted.process_slice(TimeSlice(2, active_at, (_quote(active_at),)))
    assert filled.updates[0].state is OrderState.FILLED
    assert restarted.fill_count == 1


def test_crash_after_partial_fill_retry_does_not_fill_twice(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    policy = ExecutionPolicy("half-liquidity", max_fill_fraction=Decimal("0.5"))
    session = PaperTradingSession.start(store, _config("partial-restart", policy=policy))
    session.process_slice(TimeSlice(0, NOW, (_quote(NOW, size="8"),)))
    session.queue_intent(
        _intent("partial", at=NOW, slice_index=0, quantity="10"),
        mark_price=Decimal(100),
    )
    at_one = NOW + timedelta(minutes=1)
    first = session.process_slice(TimeSlice(1, at_one, (_quote(at_one, size="8"),)))
    assert first.updates[0].state is OrderState.PARTIALLY_FILLED
    assert first.updates[0].fill is not None
    assert first.updates[0].fill.quantity == Decimal(4)
    assert session.snapshot.open_orders[0].remaining_quantity == Decimal(6)
    assert session.fill_count == 1

    restarted = PaperTradingSession.resume(SQLiteStore(tmp_path / "nika.db"), "partial-restart")
    retry = restarted.process_slice(TimeSlice(1, at_one, (_quote(at_one, size="8"),)))
    assert retry.replayed_committed_slice is True
    assert retry.updates == ()
    assert restarted.fill_count == 1
    assert restarted.snapshot.open_orders[0].remaining_quantity == Decimal(6)

    at_two = NOW + timedelta(minutes=2)
    restarted.process_slice(TimeSlice(2, at_two, (_quote(at_two, size="8"),)))
    assert restarted.fill_count == 2
    assert restarted.snapshot.open_orders[0].remaining_quantity == Decimal(2)


def test_cancelled_and_expired_orders_remain_terminal_after_restart(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    cancelled = PaperTradingSession.start(store, _config("cancelled"))
    cancelled.process_slice(TimeSlice(0, NOW, (_quote(NOW),)))
    order = cancelled.queue_intent(
        _intent("cancel-me", at=NOW, slice_index=0),
        mark_price=Decimal(100),
    )
    cancelled.cancel(order.approval_id, "test cancellation")
    cancelled = PaperTradingSession.resume(store, "cancelled")
    at_one = NOW + timedelta(minutes=1)
    cancelled.process_slice(TimeSlice(1, at_one, (_quote(at_one),)))
    assert cancelled.fill_count == 0
    assert cancelled.snapshot.terminal_orders[0].state is OrderState.CANCELLED
    assert cancelled.snapshot.terminal_orders[0].terminal_reason == "test cancellation"

    expired = PaperTradingSession.start(store, _config("expired"))
    expired.process_slice(TimeSlice(0, NOW, (_quote(NOW),)))
    expired.queue_intent(
        _intent(
            "expire-me",
            at=NOW,
            slice_index=0,
            expires_at=NOW + timedelta(seconds=30),
        ),
        mark_price=Decimal(100),
    )
    expired = PaperTradingSession.resume(store, "expired")
    expired.process_slice(TimeSlice(1, at_one, (_quote(at_one),)))
    assert expired.fill_count == 0
    assert expired.snapshot.terminal_orders[0].state is OrderState.EXPIRED

    at_two = NOW + timedelta(minutes=2)
    replay = expired.process_slice(TimeSlice(2, at_two, (_quote(at_two),)))
    assert replay.updates == ()
    assert expired.fill_count == 0


def test_restart_uses_durable_risk_limits_and_strategy_cannot_queue_approval(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    session = PaperTradingSession.start(
        store,
        _config("risk-authority", limits=_limits(max_position="1", allow_short=False)),
    )
    session.process_slice(TimeSlice(0, NOW, (_quote(NOW),)))
    restarted = PaperTradingSession.resume(store, "risk-authority")

    with pytest.raises(RiskRejected, match="position"):
        restarted.queue_intent(
            _intent("too-large", at=NOW, slice_index=0, quantity="2"),
            mark_price=Decimal(100),
        )
    assert restarted.snapshot.orders == ()
    assert not hasattr(restarted, "queue_approved_order")


def test_data_source_fingerprint_and_cutoff_are_immutable_resume_authority(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    cutoff = NOW + timedelta(minutes=10)
    data = _data(cutoff=cutoff)
    session = PaperTradingSession.start(store, _config("data-binding", data=data))
    session.process_slice(TimeSlice(0, NOW, (_quote(NOW),)))

    changed = _data(cutoff=cutoff, semantic_hash="c" * 64)
    with pytest.raises(PaperSessionConflict, match="fingerprint/cutoff"):
        PaperTradingSession.resume(store, "data-binding", expected_data=changed)

    restarted = PaperTradingSession.resume(store, "data-binding", expected_data=data)
    after_cutoff = cutoff + timedelta(seconds=1)
    with pytest.raises(PaperSessionConflict, match="data cutoff"):
        restarted.process_slice(TimeSlice(1, after_cutoff, (_quote(after_cutoff),)))


def test_slice_commit_is_atomic_when_session_state_update_crashes(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    session = PaperTradingSession.start(store, _config("atomic-slice"))
    session.process_slice(TimeSlice(0, NOW, (_quote(NOW),)))
    session.queue_intent(
        _intent("atomic-fill", at=NOW, slice_index=0, quantity="2"),
        mark_price=Decimal(100),
    )
    with store.connection() as conn:
        conn.execute(
            "CREATE TRIGGER fail_paper_session_update "
            "BEFORE UPDATE ON trading_research_paper_sessions "
            "WHEN OLD.session_id = 'atomic-slice' "
            "BEGIN SELECT RAISE(ABORT, 'simulated crash'); END"
        )

    at_one = NOW + timedelta(minutes=1)
    with pytest.raises(sqlite3.IntegrityError, match="simulated crash"):
        session.process_slice(TimeSlice(1, at_one, (_quote(at_one),)))
    with store.connection() as conn:
        conn.execute("DROP TRIGGER fail_paper_session_update")

    restarted = PaperTradingSession.resume(store, "atomic-slice")
    assert restarted.fill_count == 0
    assert restarted.snapshot.cursor_slice == 0
    assert restarted.snapshot.account.cash == Decimal(1000)
    assert restarted.snapshot.open_orders[0].remaining_quantity == Decimal(2)


def test_long_short_reversal_preserves_exact_decimal_accounting_invariants(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    policy = ExecutionPolicy("fees", fixed_fee=Decimal(1))
    session = PaperTradingSession.start(store, _config("reversal", policy=policy))
    session.process_slice(TimeSlice(0, NOW, (_quote(NOW),)))
    session.queue_intent(
        _intent("buy-five", at=NOW, slice_index=0, quantity="5"),
        mark_price=Decimal(100),
    )

    at_one = NOW + timedelta(minutes=1)
    session.process_slice(TimeSlice(1, at_one, (_quote(at_one),)))
    first = session.snapshot.account
    assert first.cash == Decimal(494)
    assert first.fees == Decimal(1)
    assert first.equity == Decimal(994)
    assert first.gross_exposure == Decimal(500)
    assert first.net_exposure == Decimal(500)
    assert first.positions[0].quantity == Decimal(5)
    assert first.positions[0].average_price == Decimal(101)

    session.queue_intent(
        _intent(
            "sell-seven",
            at=at_one,
            slice_index=1,
            quantity="7",
            side=Side.SELL,
        ),
        mark_price=Decimal(100),
    )
    at_two = NOW + timedelta(minutes=2)
    session.process_slice(TimeSlice(2, at_two, (_quote(at_two),)))
    final = session.snapshot.account
    assert final.cash == Decimal(1186)
    assert final.fees == Decimal(2)
    assert final.realized_pnl == Decimal(-10)
    assert final.unrealized_pnl == Decimal(-2)
    assert final.equity == Decimal(986)
    assert final.gross_exposure == Decimal(200)
    assert final.net_exposure == Decimal(-200)
    assert final.positions[0].quantity == Decimal(-2)
    assert final.positions[0].average_price == Decimal(99)
    assert session.snapshot.risk_state.peak_equity == Decimal(1000)


def test_same_raw_fill_identity_is_session_scoped_across_two_sessions(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    for session_id in ("isolation-a", "isolation-b"):
        session = PaperTradingSession.start(store, _config(session_id))
        session.process_slice(TimeSlice(0, NOW, (_quote(NOW),)))
        session.queue_intent(
            _intent("same-intent", at=NOW, slice_index=0),
            mark_price=Decimal(100),
        )
        at_one = NOW + timedelta(minutes=1)
        session.process_slice(TimeSlice(1, at_one, (_quote(at_one),)))
        assert session.fill_count == 1

    with store.connection() as conn:
        rows = conn.execute(
            "SELECT session_id, fill_id FROM trading_research_paper_fills "
            "WHERE session_id IN ('isolation-a', 'isolation-b') ORDER BY session_id"
        ).fetchall()
    assert len(rows) == 2
    assert rows[0]["fill_id"] != rows[1]["fill_id"]


def test_durable_config_and_terminal_state_tamper_fail_closed(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    session = PaperTradingSession.start(store, _config("tamper"))
    session.process_slice(TimeSlice(0, NOW, (_quote(NOW),)))
    session.queue_intent(
        _intent("tamper-order", at=NOW, slice_index=0),
        mark_price=Decimal(100),
    )
    with store.connection() as conn:
        conn.execute(
            "UPDATE trading_research_paper_orders SET state = 'cancelled' "
            "WHERE session_id = 'tamper'"
        )
    with pytest.raises(PaperSessionIntegrityError, match="state digest"):
        PaperTradingSession.resume(store, "tamper")

    second = PaperTradingSession.start(store, _config("tamper-config"))
    second.process_slice(TimeSlice(0, NOW, (_quote(NOW),)))
    with store.connection() as conn:
        conn.execute(
            "UPDATE trading_research_paper_sessions SET config_json = '{}' "
            "WHERE session_id = 'tamper-config'"
        )
    with pytest.raises(PaperSessionIntegrityError, match="config digest"):
        PaperTradingSession.resume(store, "tamper-config")


def test_execution_session_has_no_heldout_or_real_money_authority_surface() -> None:
    config_fields = set(PaperSessionConfig.__dataclass_fields__)
    assert config_fields == {
        "session_id",
        "strategy_id",
        "strategy_version",
        "starting_cash",
        "data",
        "execution_policy",
        "risk_limits",
    }
    assert not config_fields.intersection(
        {"heldout", "metric", "promotion", "partition", "broker", "real_money", "funding"}
    )

    tree = ast.parse(inspect.getsource(session_module))
    import_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            import_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            import_roots.add(node.module.split(".", 1)[0])
    assert import_roots.isdisjoint({"httpx", "requests", "socket", "urllib", "websockets"})
