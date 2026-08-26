from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from nika_core.trading_research import (
    ExecutionPolicy,
    Instrument,
    OrderIntent,
    OrderType,
    PortfolioLedger,
    RiskEngine,
    RiskLimits,
    RiskRejected,
    RiskState,
    Side,
    SimulatedFill,
    Venue,
)

_NOW = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)
_MARK = Decimal("99")


def _instrument() -> Instrument:
    return Instrument("NIKA-QA", Venue("SIM"), "USD")


def _risk() -> RiskEngine:
    return RiskEngine(
        RiskLimits(
            max_abs_position=Decimal("10"),
            max_gross_exposure=Decimal("1000"),
            max_net_exposure=Decimal("1000"),
            max_session_loss=Decimal("1000"),
            max_drawdown=Decimal("1000"),
            allow_short=False,
            max_leverage=Decimal("1"),
        )
    )


def _intent(
    intent_id: str,
    instrument: Instrument,
    quantity: Decimal,
) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        instrument=instrument,
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=quantity,
        submitted_at=_NOW,
        submitted_slice=1,
    )


def _fill(
    fill_id: str,
    intent: OrderIntent,
    *,
    fee: Decimal,
) -> SimulatedFill:
    return SimulatedFill(
        fill_id=fill_id,
        approval_id=f"risk:{intent.intent_id}",
        intent_id=intent.intent_id,
        instrument=intent.instrument,
        side=intent.side,
        quantity=intent.quantity,
        price=_MARK,
        fee=fee,
        filled_at=_NOW,
        filled_slice=2,
    )


def _initial_snapshot():
    return PortfolioLedger(Decimal("100")).snapshot({})


def _risk_state() -> RiskState:
    return RiskState(
        peak_equity=Decimal("100"),
        session_start_equity=Decimal("100"),
    )


def test_known_single_order_fee_cannot_be_approved_into_immediate_leverage_breach() -> None:
    instrument = _instrument()
    risk = _risk()
    policy = ExecutionPolicy("fee-breach", fixed_fee=Decimal("2"))
    intent = _intent("single-fee-breach", instrument, Decimal("1"))

    post_fill_ledger = PortfolioLedger(Decimal("100"))
    post_fill_ledger.apply_fill(_fill("fill-single", intent, fee=policy.fixed_fee))
    breached = post_fill_ledger.snapshot({instrument.instrument_id: _MARK})
    with pytest.raises(RiskRejected, match="post-fill leverage breach"):
        risk.assert_post_fill(breached, _risk_state())

    with pytest.raises(RiskRejected):
        risk.approve(
            intent,
            snapshot=_initial_snapshot(),
            mark_price=_MARK,
            pending_signed_quantity=Decimal("0"),
            approved_at=_NOW,
            approved_slice=1,
            policy=policy,
            risk_state=_risk_state(),
        )


def test_pending_order_costs_are_reserved_before_second_approval() -> None:
    instrument = _instrument()
    risk = _risk()
    policy = ExecutionPolicy("pending-fee-breach", fixed_fee=Decimal("1"))
    first_intent = _intent("pending-a", instrument, Decimal("0.5"))
    second_intent = _intent("pending-b", instrument, Decimal("0.5"))

    first = risk.approve(
        first_intent,
        snapshot=_initial_snapshot(),
        mark_price=_MARK,
        pending_signed_quantity=Decimal("0"),
        approved_at=_NOW,
        approved_slice=1,
        policy=policy,
        risk_state=_risk_state(),
    )
    assert first.intent.intent_id == first_intent.intent_id

    combined_ledger = PortfolioLedger(Decimal("100"))
    combined_ledger.apply_fill(_fill("fill-a", first_intent, fee=policy.fixed_fee))
    combined_ledger.apply_fill(_fill("fill-b", second_intent, fee=policy.fixed_fee))
    breached = combined_ledger.snapshot({instrument.instrument_id: _MARK})
    with pytest.raises(RiskRejected, match="post-fill leverage breach"):
        risk.assert_post_fill(breached, _risk_state())

    with pytest.raises(RiskRejected):
        risk.approve(
            second_intent,
            snapshot=_initial_snapshot(),
            mark_price=_MARK,
            pending_signed_quantity=first_intent.quantity,
            approved_at=_NOW,
            approved_slice=1,
            policy=policy,
            risk_state=_risk_state(),
        )


def test_fee_boundary_that_keeps_leverage_at_limit_remains_approvable() -> None:
    instrument = _instrument()
    risk = _risk()
    policy = ExecutionPolicy("fee-boundary", fixed_fee=Decimal("1"))
    intent = _intent("fee-boundary", instrument, Decimal("1"))

    approved = risk.approve(
        intent,
        snapshot=_initial_snapshot(),
        mark_price=_MARK,
        pending_signed_quantity=Decimal("0"),
        approved_at=_NOW,
        approved_slice=1,
        policy=policy,
        risk_state=_risk_state(),
    )
    assert approved.intent.intent_id == intent.intent_id

    ledger = PortfolioLedger(Decimal("100"))
    ledger.apply_fill(_fill("fill-boundary", intent, fee=policy.fixed_fee))
    safe_snapshot = ledger.snapshot({instrument.instrument_id: _MARK})
    assert safe_snapshot.gross_exposure / safe_snapshot.equity == Decimal("1")
    risk.assert_post_fill(safe_snapshot, _risk_state())
