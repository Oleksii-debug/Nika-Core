from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from nika_core.trading_research import PendingRiskOrder
from nika_core.trading_research.accounting import AccountSnapshot, Position
from nika_core.trading_research.contracts import EventTime, Instrument, Quote, Venue
from nika_core.trading_research.orders import (
    ExecutionPolicy,
    OrderIntent,
    OrderType,
    RiskApprovedOrder,
    Side,
)
from nika_core.trading_research.replay import SimulationExecutionEngine, TimeSlice
from nika_core.trading_research.risk import RiskEngine, RiskLimits, RiskRejected, RiskState

_NOW = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)
_INSTRUMENT = Instrument("NIKA-RISK", Venue("SIM", "UTC"), "USD")


def _snapshot(*, cash: Decimal = Decimal(100), equity: Decimal = Decimal(100)) -> AccountSnapshot:
    return AccountSnapshot(
        cash=cash,
        fees=Decimal(0),
        realized_pnl=Decimal(0),
        unrealized_pnl=Decimal(0),
        equity=equity,
        gross_exposure=Decimal(0),
        net_exposure=Decimal(0),
        positions=(),
    )


def _risk(
    *,
    max_leverage: Decimal = Decimal(1),
    max_abs_position: Decimal = Decimal(10),
) -> RiskEngine:
    return RiskEngine(
        RiskLimits(
            max_abs_position=max_abs_position,
            max_gross_exposure=Decimal(1000),
            max_net_exposure=Decimal(1000),
            max_session_loss=Decimal(1000),
            max_drawdown=Decimal(1000),
            allow_short=False,
            max_leverage=max_leverage,
        )
    )


def _intent(intent_id: str, quantity: Decimal) -> OrderIntent:
    return OrderIntent(
        intent_id=intent_id,
        instrument=_INSTRUMENT,
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=quantity,
        submitted_at=_NOW,
        submitted_slice=1,
    )


def _state() -> RiskState:
    return RiskState(
        peak_equity=Decimal(100),
        session_start_equity=Decimal(100),
    )


def _approve(
    risk: RiskEngine,
    intent: OrderIntent,
    *,
    policy: ExecutionPolicy,
    snapshot: AccountSnapshot | None = None,
    pending_signed_quantity: Decimal = Decimal(0),
    pending_orders: tuple[PendingRiskOrder, ...] = (),
):
    return risk.approve(
        intent,
        snapshot=_snapshot() if snapshot is None else snapshot,
        mark_price=Decimal(99),
        pending_signed_quantity=pending_signed_quantity,
        approved_at=_NOW,
        approved_slice=1,
        policy=policy,
        risk_state=_state(),
        pending_orders=pending_orders,
    )


def test_candidate_fee_is_reserved_before_leverage_approval() -> None:
    with pytest.raises(RiskRejected):
        _approve(
            _risk(),
            _intent("fee-breach", Decimal(1)),
            policy=ExecutionPolicy("fee-breach", fixed_fee=Decimal(2)),
        )


def test_fee_boundary_at_exact_leverage_limit_remains_approvable() -> None:
    approved = _approve(
        _risk(),
        _intent("fee-boundary", Decimal(1)),
        policy=ExecutionPolicy("fee-boundary", fixed_fee=Decimal(1)),
    )
    assert approved.intent.intent_id == "fee-boundary"


def test_costed_legacy_pending_quantity_fails_closed_without_exact_order_authority() -> None:
    with pytest.raises(RiskRejected, match="exact pending execution-cost reservation required"):
        _approve(
            _risk(),
            _intent("legacy-pending", Decimal("0.5")),
            policy=ExecutionPolicy("costed", fixed_fee=Decimal(1)),
            pending_signed_quantity=Decimal("0.5"),
        )


def test_exact_pending_order_costs_are_reserved_before_second_approval() -> None:
    risk = _risk()
    policy = ExecutionPolicy("pending-cost", fixed_fee=Decimal(1))
    first = _approve(
        risk,
        _intent("pending-a", Decimal("0.5")),
        policy=policy,
    )

    with pytest.raises(RiskRejected):
        _approve(
            risk,
            _intent("pending-b", Decimal("0.5")),
            policy=policy,
            pending_orders=(PendingRiskOrder(first, Decimal(99)),),
        )


def test_cash_reservation_rejects_order_even_when_leverage_limit_would_allow_it() -> None:
    with pytest.raises(RiskRejected, match="insufficient cash"):
        _approve(
            _risk(max_leverage=Decimal(2)),
            _intent("cash-breach", Decimal(1)),
            policy=ExecutionPolicy("cash-breach", fixed_fee=Decimal(2)),
        )


def test_pending_remaining_quantity_avoids_double_counting_already_filled_exposure() -> None:
    policy = ExecutionPolicy("partial-pending", fixed_fee=Decimal(1))
    original = _intent("partial-parent", Decimal(1))
    approved = RiskApprovedOrder("risk:partial-parent", original, _NOW, 1, policy)
    position = Position(_INSTRUMENT, Decimal("0.5"), Decimal(99), Decimal(0))
    snapshot = AccountSnapshot(
        cash=Decimal(100),
        fees=Decimal(1),
        realized_pnl=Decimal(0),
        unrealized_pnl=Decimal(0),
        equity=Decimal(100),
        gross_exposure=Decimal("49.5"),
        net_exposure=Decimal("49.5"),
        positions=(position,),
    )

    result = _approve(
        _risk(max_leverage=Decimal(2), max_abs_position=Decimal(1)),
        _intent("after-partial", Decimal("0.25")),
        policy=policy,
        snapshot=snapshot,
        pending_orders=(
            PendingRiskOrder(
                approved,
                Decimal(99),
                remaining_quantity=Decimal("0.25"),
            ),
        ),
    )
    assert result.intent.intent_id == "after-partial"


def test_fixed_fee_is_charged_once_across_partial_fills() -> None:
    intent = OrderIntent(
        intent_id="partial-fee",
        instrument=_INSTRUMENT,
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=Decimal(2),
        submitted_at=_NOW,
        submitted_slice=0,
    )
    order = RiskApprovedOrder(
        "risk:partial-fee",
        intent,
        _NOW,
        0,
        ExecutionPolicy("one-fixed", fixed_fee=Decimal(1)),
    )
    execution = SimulationExecutionEngine()
    quote = Quote(
        _INSTRUMENT,
        EventTime(_NOW, _NOW, _NOW),
        Decimal(98),
        Decimal(99),
        Decimal(1),
        Decimal(1),
    )

    first = execution.execute(order, TimeSlice(1, _NOW, (quote,)))
    assert first.fill is not None
    assert first.fill.quantity == Decimal(1)
    assert first.fill.fee == Decimal(1)
    assert first.remaining_quantity == Decimal(1)

    second = execution.execute(
        order,
        TimeSlice(2, _NOW, (quote,)),
        remaining_quantity=first.remaining_quantity,
    )
    assert second.fill is not None
    assert second.fill.quantity == Decimal(1)
    assert second.fill.fee == Decimal(0)
    assert second.remaining_quantity == Decimal(0)
