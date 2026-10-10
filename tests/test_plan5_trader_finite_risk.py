"""Plan 5 Trader: reject forged non-finite authority before simulated exposure."""
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from nika_core.trading_research.accounting import AccountSnapshot
from nika_core.trading_research.contracts import Instrument, TradingResearchError, Venue
from nika_core.trading_research.orders import (
    ExecutionPolicy, OrderAuthority, OrderIntent, OrderType, Side,
)
from nika_core.trading_research.risk import (
    PendingRiskOrder, RiskEngine, RiskLimits, RiskState,
)

NOW = datetime(2026, 8, 26, tzinfo=UTC)
INSTRUMENT = Instrument("RISK-FINITE", Venue("PAPER", "UTC"), "USD")


def limits() -> RiskLimits:
    return RiskLimits(
        max_abs_position=Decimal(10),
        max_gross_exposure=Decimal(1000),
        max_net_exposure=Decimal(1000),
        max_session_loss=Decimal(1000),
        max_drawdown=Decimal(1000),
    )


def snapshot() -> AccountSnapshot:
    return AccountSnapshot(
        cash=Decimal(1000), fees=Decimal(0),
        realized_pnl=Decimal(0), unrealized_pnl=Decimal(0),
        equity=Decimal(1000), gross_exposure=Decimal(0),
        net_exposure=Decimal(0), positions=(),
    )


def approve(*, snap=None, mark=Decimal(100), pending=Decimal(0),
            state=None, policy=None, intent=None, engine=None):
    order = intent or OrderIntent(
        "finite-order", INSTRUMENT, Side.BUY, OrderType.MARKET,
        Decimal(1), NOW, 0,
    )
    return (engine or RiskEngine(limits())).approve(
        order,
        authority=OrderAuthority("paper-workspace", "paper-run", "order-1", NOW, 0),
        snapshot=snap if snap is not None else snapshot(),
        mark_price=mark,
        pending_signed_quantity=pending,
        approved_at=NOW,
        approved_slice=0,
        policy=policy or ExecutionPolicy("finite-policy"),
        risk_state=state or RiskState(Decimal(1000), Decimal(1000)),
    )


@pytest.mark.parametrize("field", (
    "max_abs_position", "max_gross_exposure", "max_net_exposure",
    "max_session_loss", "max_drawdown", "max_leverage",
))
@pytest.mark.parametrize("invalid", (
    Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity"), 1.0, True,
))
def test_risk_limits_are_strict_finite_decimals(field, invalid):
    with pytest.raises(TradingResearchError):
        replace(limits(), **{field: invalid})


@pytest.mark.parametrize("field", ("peak_equity", "session_start_equity"))
@pytest.mark.parametrize("invalid", (Decimal("NaN"), Decimal("Infinity"), 1.0))
def test_risk_anchor_rejects_non_finite_and_non_decimal(field, invalid):
    with pytest.raises(TradingResearchError):
        replace(RiskState(Decimal(1000), Decimal(1000)), **{field: invalid})


@pytest.mark.parametrize("field", (
    "cash", "fees", "realized_pnl", "unrealized_pnl", "equity",
    "gross_exposure", "net_exposure",
))
@pytest.mark.parametrize("invalid", (Decimal("NaN"), Decimal("Infinity"), 1.0))
def test_approval_rejects_forged_snapshot(field, invalid):
    with pytest.raises(TradingResearchError):
        approve(snap=replace(snapshot(), **{field: invalid}))


@pytest.mark.parametrize("invalid", (Decimal("NaN"), Decimal("Infinity"), 1.0))
def test_approval_rejects_invalid_market_marks_and_reservations(invalid):
    with pytest.raises(TradingResearchError):
        approve(mark=invalid)
    with pytest.raises(TradingResearchError):
        approve(pending=invalid)


@pytest.mark.parametrize("invalid", (Decimal("NaN"), Decimal("Infinity"), 1.0))
def test_pending_order_reservation_rejects_non_finite_mark(invalid):
    # The pending mark cannot be used to obtain a free/bypassing reservation.
    from nika_core.trading_research.orders import RiskApprovedOrder
    order = OrderIntent("pending", INSTRUMENT, Side.BUY, OrderType.MARKET,
                        Decimal(1), NOW, 0)
    authority = OrderAuthority("paper-workspace", "paper-run", "p", NOW, 0)
    approved = RiskApprovedOrder("approved", order, authority, NOW, 0,
                                 ExecutionPolicy("policy"))
    with pytest.raises(TradingResearchError):
        PendingRiskOrder(approved, invalid)


def test_mutated_limits_and_state_fail_at_use_boundary():
    rules = limits()
    engine = RiskEngine(rules)
    object.__setattr__(rules, "max_gross_exposure", Decimal("Infinity"))
    with pytest.raises(TradingResearchError):
        approve(engine=engine)

    state = RiskState(Decimal(1000), Decimal(1000))
    object.__setattr__(state, "peak_equity", Decimal("NaN"))
    with pytest.raises(TradingResearchError):
        approve(state=state)


def test_post_fill_rejects_forged_snapshot():
    with pytest.raises(TradingResearchError):
        RiskEngine(limits()).assert_post_fill(
            replace(snapshot(), equity=Decimal("Infinity")),
            RiskState(Decimal(1000), Decimal(1000)),
        )


def test_valid_paper_risk_approval_still_succeeds():
    approved = approve()
    assert approved.authority.workspace_id == "paper-workspace"
    assert approved.intent.quantity == Decimal(1)


@pytest.mark.parametrize("field", (
    "slippage_bps", "fee_bps", "fixed_fee", "max_fill_fraction",
))
@pytest.mark.parametrize("invalid", (Decimal("Infinity"), Decimal("NaN")))
def test_policy_numeric_bypass_is_rejected_before_approval(field, invalid):
    policy = replace(ExecutionPolicy("malformed"), **{field: invalid})
    with pytest.raises(TradingResearchError):
        approve(policy=policy)


def test_strategy_cannot_approve_infinite_order_quantity():
    intent = OrderIntent(
        "unbounded", INSTRUMENT, Side.BUY, OrderType.MARKET,
        Decimal("Infinity"), NOW, 0,
    )
    with pytest.raises(TradingResearchError):
        approve(intent=intent)


def test_nonfinite_position_in_forged_snapshot_fails_closed():
    from nika_core.trading_research.accounting import Position
    position = Position(INSTRUMENT, quantity=Decimal("Infinity"))
    with pytest.raises(TradingResearchError):
        approve(snap=replace(snapshot(), positions=(position,)))
