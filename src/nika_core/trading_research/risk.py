from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .accounting import AccountSnapshot
from .contracts import TradingResearchError, require_aware_utc
from .orders import (
    ExecutionPolicy,
    OrderIntent,
    OrderType,
    RiskApprovedOrder,
    Side,
    apply_slippage,
    fee_for,
)


class RiskRejected(TradingResearchError):
    """Raised when a simulated order breaches an explicit research risk limit."""


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_abs_position: Decimal
    max_gross_exposure: Decimal
    max_net_exposure: Decimal
    max_session_loss: Decimal
    max_drawdown: Decimal
    allow_short: bool = False
    max_leverage: Decimal = Decimal(1)

    def __post_init__(self) -> None:
        values = (
            self.max_abs_position,
            self.max_gross_exposure,
            self.max_net_exposure,
            self.max_session_loss,
            self.max_drawdown,
            self.max_leverage,
        )
        if any(value < 0 for value in values):
            raise TradingResearchError("risk limits cannot be negative")
        if self.max_leverage == 0:
            raise TradingResearchError("max_leverage must be positive")


@dataclass(frozen=True, slots=True)
class RiskState:
    peak_equity: Decimal
    session_start_equity: Decimal

    def __post_init__(self) -> None:
        if self.peak_equity < 0 or self.session_start_equity < 0:
            raise TradingResearchError("risk equity anchors cannot be negative")


@dataclass(frozen=True, slots=True)
class PendingRiskOrder:
    """Accepted pending order plus the mark used for this admission decision."""

    order: RiskApprovedOrder
    mark_price: Decimal

    def __post_init__(self) -> None:
        if self.mark_price <= 0:
            raise TradingResearchError("pending mark_price must be positive")


@dataclass(frozen=True, slots=True)
class _ExecutionReservation:
    equity_cost: Decimal
    cash_required: Decimal


class RiskEngine:
    def __init__(self, limits: RiskLimits) -> None:
        self._limits = limits

    def approve(
        self,
        intent: OrderIntent,
        *,
        snapshot: AccountSnapshot,
        mark_price: Decimal,
        pending_signed_quantity: Decimal,
        approved_at: datetime,
        approved_slice: int,
        policy: ExecutionPolicy,
        risk_state: RiskState,
        pending_orders: tuple[PendingRiskOrder, ...] = (),
    ) -> RiskApprovedOrder:
        approved_at = require_aware_utc(approved_at, "approved_at")
        if mark_price <= 0:
            raise TradingResearchError("mark_price must be positive")
        if pending_orders and pending_signed_quantity != 0:
            raise TradingResearchError(
                "use pending_orders or legacy pending_signed_quantity, not both"
            )

        marks: dict[str, Decimal] = {}
        deltas: dict[str, Decimal] = {}
        total_equity_cost = Decimal(0)
        total_cash_required = Decimal(0)

        seen_pending_approvals: set[str] = set()
        for pending in pending_orders:
            if pending.order.approval_id in seen_pending_approvals:
                raise TradingResearchError("duplicate pending approval_id")
            seen_pending_approvals.add(pending.order.approval_id)
            if (
                pending.order.approved_at > approved_at
                or pending.order.approved_slice > approved_slice
            ):
                raise TradingResearchError("pending order cannot come from a future decision")
            pending_intent = pending.order.intent
            key = pending_intent.instrument.instrument_id
            _record_mark(marks, key, pending.mark_price)
            _add_delta(
                deltas,
                key,
                pending_intent.quantity * Decimal(pending_intent.side.sign),
            )
            reservation = _execution_reservation(
                pending_intent,
                pending.mark_price,
                pending.order.policy,
            )
            total_equity_cost += reservation.equity_cost
            total_cash_required += reservation.cash_required

        candidate_key = intent.instrument.instrument_id
        _record_mark(marks, candidate_key, mark_price)

        if pending_signed_quantity != 0:
            if _policy_has_execution_cost(policy):
                raise RiskRejected(
                    "exact pending execution-cost reservation required"
                )
            _add_delta(deltas, candidate_key, pending_signed_quantity)
            legacy_reservation = _legacy_pending_reservation(
                pending_signed_quantity,
                mark_price,
                policy,
            )
            total_cash_required += legacy_reservation.cash_required

        signed = intent.quantity * Decimal(intent.side.sign)
        _add_delta(deltas, candidate_key, signed)
        candidate_reservation = _execution_reservation(intent, mark_price, policy)
        total_equity_cost += candidate_reservation.equity_cost
        total_cash_required += candidate_reservation.cash_required

        if total_cash_required > snapshot.cash:
            raise RiskRejected("insufficient cash for deterministic execution reservation")

        projected_net = snapshot.net_exposure
        projected_gross = snapshot.gross_exposure
        for instrument_id, delta in deltas.items():
            current_qty = _position_quantity(snapshot, instrument_id)
            projected_qty = current_qty + delta
            if not self._limits.allow_short and projected_qty < 0:
                raise RiskRejected("short positions are disabled")
            if abs(projected_qty) > self._limits.max_abs_position:
                raise RiskRejected("max_abs_position exceeded")

            instrument_mark = marks[instrument_id]
            current_value = current_qty * instrument_mark
            projected_value = projected_qty * instrument_mark
            projected_net += projected_value - current_value
            projected_gross += abs(projected_value) - abs(current_value)

        if projected_gross > self._limits.max_gross_exposure:
            raise RiskRejected("max_gross_exposure exceeded")
        if abs(projected_net) > self._limits.max_net_exposure:
            raise RiskRejected("max_net_exposure exceeded")

        projected_equity = snapshot.equity - total_equity_cost
        if projected_equity <= 0 and projected_gross > 0:
            raise RiskRejected("positive projected equity required for exposure")
        if (
            projected_equity > 0
            and projected_gross / projected_equity > self._limits.max_leverage
        ):
            raise RiskRejected("max_leverage exceeded")

        session_loss = max(
            Decimal(0),
            risk_state.session_start_equity - projected_equity,
        )
        drawdown = max(Decimal(0), risk_state.peak_equity - projected_equity)
        if session_loss >= self._limits.max_session_loss:
            raise RiskRejected("max_session_loss reached")
        if drawdown >= self._limits.max_drawdown:
            raise RiskRejected("max_drawdown reached")

        return RiskApprovedOrder(
            approval_id=f"risk:{intent.intent_id}",
            intent=intent,
            approved_at=approved_at,
            approved_slice=approved_slice,
            policy=policy,
        )

    def assert_post_fill(self, snapshot: AccountSnapshot, risk_state: RiskState) -> None:
        for position in snapshot.positions:
            if not self._limits.allow_short and position.quantity < 0:
                raise RiskRejected("post-fill short position breach")
            if abs(position.quantity) > self._limits.max_abs_position:
                raise RiskRejected("post-fill position breach")
        if snapshot.gross_exposure > self._limits.max_gross_exposure:
            raise RiskRejected("post-fill gross exposure breach")
        if abs(snapshot.net_exposure) > self._limits.max_net_exposure:
            raise RiskRejected("post-fill net exposure breach")
        if snapshot.equity <= 0 and snapshot.gross_exposure > 0:
            raise RiskRejected("post-fill non-positive equity with exposure")
        if (
            snapshot.equity > 0
            and snapshot.gross_exposure / snapshot.equity > self._limits.max_leverage
        ):
            raise RiskRejected("post-fill leverage breach")
        session_loss = max(Decimal(0), risk_state.session_start_equity - snapshot.equity)
        drawdown = max(Decimal(0), risk_state.peak_equity - snapshot.equity)
        if session_loss > self._limits.max_session_loss:
            raise RiskRejected("post-fill session loss breach")
        if drawdown > self._limits.max_drawdown:
            raise RiskRejected("post-fill drawdown breach")


def _record_mark(marks: dict[str, Decimal], instrument_id: str, mark_price: Decimal) -> None:
    existing = marks.get(instrument_id)
    if existing is not None and existing != mark_price:
        raise TradingResearchError(
            f"inconsistent risk marks for instrument {instrument_id}"
        )
    marks[instrument_id] = mark_price


def _add_delta(deltas: dict[str, Decimal], instrument_id: str, quantity: Decimal) -> None:
    deltas[instrument_id] = deltas.get(instrument_id, Decimal(0)) + quantity


def _policy_has_execution_cost(policy: ExecutionPolicy) -> bool:
    return (
        policy.slippage_bps != 0
        or policy.fee_bps != 0
        or policy.fixed_fee != 0
    )


def _execution_reservation(
    intent: OrderIntent,
    mark_price: Decimal,
    policy: ExecutionPolicy,
) -> _ExecutionReservation:
    fill_price = _worst_case_fill_price(intent, mark_price, policy)
    notional = intent.quantity * fill_price
    fee = fee_for(notional, policy)

    if intent.side is Side.BUY:
        adverse_price_loss = max(Decimal(0), fill_price - mark_price) * intent.quantity
        cash_required = notional + fee
    else:
        adverse_price_loss = max(Decimal(0), mark_price - fill_price) * intent.quantity
        cash_required = max(Decimal(0), fee - notional)

    return _ExecutionReservation(
        equity_cost=adverse_price_loss + fee,
        cash_required=cash_required,
    )


def _legacy_pending_reservation(
    signed_quantity: Decimal,
    mark_price: Decimal,
    policy: ExecutionPolicy,
) -> _ExecutionReservation:
    quantity = abs(signed_quantity)
    if quantity == 0:
        return _ExecutionReservation(Decimal(0), Decimal(0))
    side = Side.BUY if signed_quantity > 0 else Side.SELL
    notional = quantity * apply_slippage(mark_price, side, policy.slippage_bps)
    fee = fee_for(notional, policy)
    if side is Side.BUY:
        return _ExecutionReservation(
            equity_cost=Decimal(0),
            cash_required=notional + fee,
        )
    return _ExecutionReservation(
        equity_cost=Decimal(0),
        cash_required=max(Decimal(0), fee - notional),
    )


def _worst_case_fill_price(
    intent: OrderIntent,
    mark_price: Decimal,
    policy: ExecutionPolicy,
) -> Decimal:
    if intent.order_type is OrderType.LIMIT:
        if intent.limit_price is None:
            raise TradingResearchError("limit order missing limit_price")
        return intent.limit_price
    return apply_slippage(mark_price, intent.side, policy.slippage_bps)


def _position_quantity(snapshot: AccountSnapshot, instrument_id: str) -> Decimal:
    for position in snapshot.positions:
        if position.instrument.instrument_id == instrument_id:
            return position.quantity
    return Decimal(0)
