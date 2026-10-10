from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .accounting import AccountSnapshot, Position
from .contracts import TradingResearchError, require_aware_utc
from .identity import InstrumentIdentity, instrument_identity, instrument_identity_sha256
from .orders import (
    ExecutionPolicy,
    OrderAuthority,
    OrderIntent,
    OrderType,
    RiskApprovedOrder,
    Side,
    apply_slippage,
    fee_for,
    order_authority_sha256,
)


class RiskRejected(TradingResearchError):
    """Raised when a simulated order breaches an explicit research risk limit."""


def _finite_decimal(value: object, name: str) -> Decimal:
    """Risk authority never accepts NaN, infinity, floats, or coerced numbers."""
    if type(value) is not Decimal or not value.is_finite():
        raise TradingResearchError(f"{name} must be a finite Decimal")
    return value


def _validate_policy(policy: ExecutionPolicy) -> None:
    if type(policy) is not ExecutionPolicy:
        raise TradingResearchError("risk execution policy must be ExecutionPolicy")
    for name in ("slippage_bps", "fee_bps", "fixed_fee", "max_fill_fraction"):
        _finite_decimal(getattr(policy, name), f"execution policy {name}")
    # Immutable carriers can be forged through object.__setattr__; recheck at use.
    policy.__post_init__()


def _validate_snapshot(snapshot: AccountSnapshot) -> None:
    if type(snapshot) is not AccountSnapshot:
        raise TradingResearchError("risk snapshot must be AccountSnapshot")
    for name in (
        "cash", "fees", "realized_pnl", "unrealized_pnl", "equity",
        "gross_exposure", "net_exposure",
    ):
        _finite_decimal(getattr(snapshot, name), f"snapshot {name}")
    if type(snapshot.positions) is not tuple:
        raise TradingResearchError("risk snapshot positions must be a tuple")
    if snapshot.fees < 0:
        raise TradingResearchError("risk snapshot fees cannot be negative")
    if snapshot.gross_exposure < 0 or (
        snapshot.gross_exposure < abs(snapshot.net_exposure)
    ):
        raise TradingResearchError("risk snapshot gross/net exposure is inconsistent")
    seen: set[InstrumentIdentity] = set()
    for position in snapshot.positions:
        if type(position) is not Position:
            raise TradingResearchError("risk snapshot positions must be Position")
        identity = instrument_identity(position.instrument)
        if identity in seen:
            raise TradingResearchError("risk snapshot has duplicate instrument identity")
        seen.add(identity)
        for name in ("quantity", "average_price", "realized_pnl"):
            _finite_decimal(getattr(position, name), f"position {name}")


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
        for value, name in zip(
            values,
            ("max_abs_position", "max_gross_exposure", "max_net_exposure",
             "max_session_loss", "max_drawdown", "max_leverage"),
            strict=True,
        ):
            _finite_decimal(value, f"risk {name}")
            if value < 0:
                raise TradingResearchError("risk limits cannot be negative")
        if self.max_leverage == 0:
            raise TradingResearchError("max_leverage must be positive")
        if type(self.allow_short) is not bool:
            raise TradingResearchError("allow_short must be boolean")


@dataclass(frozen=True, slots=True)
class RiskState:
    peak_equity: Decimal
    session_start_equity: Decimal

    def __post_init__(self) -> None:
        for name in ("peak_equity", "session_start_equity"):
            value = _finite_decimal(getattr(self, name), name)
            if value < 0:
                raise TradingResearchError("risk equity anchors cannot be negative")


@dataclass(frozen=True, slots=True)
class PendingRiskOrder:
    """Accepted pending order plus exact remaining quantity and current admission mark."""

    order: RiskApprovedOrder
    mark_price: Decimal
    remaining_quantity: Decimal | None = None

    def __post_init__(self) -> None:
        mark = _finite_decimal(self.mark_price, "pending mark_price")
        if mark <= 0:
            raise TradingResearchError("pending mark_price must be positive")
        remaining = (
            self.order.intent.quantity
            if self.remaining_quantity is None
            else self.remaining_quantity
        )
        _finite_decimal(remaining, "pending remaining_quantity")
        _finite_decimal(self.order.intent.quantity, "pending order quantity")
        if remaining <= 0 or remaining > self.order.intent.quantity:
            raise TradingResearchError("pending remaining_quantity must be in (0, order quantity]")
        object.__setattr__(self, "remaining_quantity", remaining)


@dataclass(frozen=True, slots=True)
class _ExecutionReservation:
    equity_cost: Decimal
    cash_required: Decimal


class RiskEngine:
    def __init__(self, limits: RiskLimits) -> None:
        if type(limits) is not RiskLimits:
            raise TradingResearchError("risk limits must be RiskLimits")
        limits.__post_init__()
        self._limits = limits

    def approve(
        self,
        intent: OrderIntent,
        *,
        authority: OrderAuthority,
        snapshot: AccountSnapshot,
        mark_price: Decimal,
        pending_signed_quantity: Decimal,
        approved_at: datetime,
        approved_slice: int,
        policy: ExecutionPolicy,
        risk_state: RiskState,
        pending_orders: tuple[PendingRiskOrder, ...] = (),
    ) -> RiskApprovedOrder:
        self._limits.__post_init__()
        _validate_snapshot(snapshot)
        if type(risk_state) is not RiskState:
            raise TradingResearchError("approve requires RiskState")
        risk_state.__post_init__()
        if type(intent) is not OrderIntent or type(policy) is not ExecutionPolicy:
            raise TradingResearchError("approve requires validated intent and execution policy")
        _finite_decimal(intent.quantity, "intent quantity")
        if type(approved_slice) is not int or approved_slice < 0:
            raise TradingResearchError("approved_slice must be non-negative integer")
        approved_at = require_aware_utc(approved_at, "approved_at")
        if type(authority) is not OrderAuthority:
            raise TradingResearchError("approve requires host OrderAuthority")
        _finite_decimal(mark_price, "mark_price")
        _finite_decimal(pending_signed_quantity, "pending_signed_quantity")
        _validate_policy(policy)
        if mark_price <= 0:
            raise TradingResearchError("mark_price must be positive")
        if pending_orders and pending_signed_quantity != 0:
            raise TradingResearchError(
                "use pending_orders or legacy pending_signed_quantity, not both"
            )

        marks: dict[InstrumentIdentity, Decimal] = {}
        deltas: dict[InstrumentIdentity, Decimal] = {}
        total_equity_cost = Decimal(0)
        total_cash_required = Decimal(0)

        seen_pending_approvals: set[str] = set()
        for pending in pending_orders:
            if type(pending) is not PendingRiskOrder:
                raise TradingResearchError("pending orders must be PendingRiskOrder")
            if type(pending.order) is not RiskApprovedOrder:
                raise TradingResearchError("pending order must be RiskApprovedOrder")
            pending.__post_init__()
            _validate_policy(pending.order.policy)
            if pending.order.approval_id in seen_pending_approvals:
                raise TradingResearchError("duplicate pending approval_id")
            seen_pending_approvals.add(pending.order.approval_id)
            if (
                pending.order.authority.workspace_id != authority.workspace_id
                or pending.order.authority.run_id != authority.run_id
            ):
                raise TradingResearchError("pending order belongs to another workspace/run")
            if (
                pending.order.approved_at > approved_at
                or pending.order.approved_slice > approved_slice
            ):
                raise TradingResearchError("pending order cannot come from a future decision")
            pending_intent = pending.order.intent
            key = instrument_identity(pending_intent.instrument)
            _record_mark(marks, key, pending.mark_price)
            remaining = pending.remaining_quantity
            assert remaining is not None
            _add_delta(
                deltas,
                key,
                remaining * Decimal(pending_intent.side.sign),
            )
            reservation = _execution_reservation(
                pending_intent,
                pending.mark_price,
                pending.order.policy,
                quantity=remaining,
                include_fixed_fee=remaining == pending_intent.quantity,
            )
            total_equity_cost += reservation.equity_cost
            total_cash_required += reservation.cash_required

        candidate_key = instrument_identity(intent.instrument)
        _record_mark(marks, candidate_key, mark_price)

        if pending_signed_quantity != 0:
            if _policy_has_execution_cost(policy):
                raise RiskRejected("exact pending execution-cost reservation required")
            _add_delta(deltas, candidate_key, pending_signed_quantity)
            legacy_reservation = _legacy_pending_reservation(
                pending_signed_quantity,
                mark_price,
                policy,
            )
            total_cash_required += legacy_reservation.cash_required

        signed = intent.quantity * Decimal(intent.side.sign)
        _add_delta(deltas, candidate_key, signed)
        candidate_reservation = _execution_reservation(
            intent,
            mark_price,
            policy,
            quantity=intent.quantity,
            include_fixed_fee=True,
        )
        total_equity_cost += candidate_reservation.equity_cost
        total_cash_required += candidate_reservation.cash_required

        if total_cash_required > snapshot.cash:
            raise RiskRejected("insufficient cash for deterministic execution reservation")

        projected_net = snapshot.net_exposure
        projected_gross = snapshot.gross_exposure
        for identity, delta in deltas.items():
            current_qty = _position_quantity(snapshot, identity)
            projected_qty = current_qty + delta
            if not self._limits.allow_short and projected_qty < 0:
                raise RiskRejected("short positions are disabled")
            if abs(projected_qty) > self._limits.max_abs_position:
                raise RiskRejected("max_abs_position exceeded")

            instrument_mark = marks[identity]
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
            approval_id=(
                f"risk:{order_authority_sha256(authority)}:"
                f"{instrument_identity_sha256(intent.instrument)}"
            ),
            intent=intent,
            authority=authority,
            approved_at=approved_at,
            approved_slice=approved_slice,
            policy=policy,
        )

    def assert_post_fill(self, snapshot: AccountSnapshot, risk_state: RiskState) -> None:
        self._limits.__post_init__()
        _validate_snapshot(snapshot)
        if type(risk_state) is not RiskState:
            raise TradingResearchError("post-fill requires RiskState")
        risk_state.__post_init__()
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


def _record_mark(
    marks: dict[InstrumentIdentity, Decimal],
    identity: InstrumentIdentity,
    mark_price: Decimal,
) -> None:
    _finite_decimal(mark_price, "risk mark")
    existing = marks.get(identity)
    if existing is not None and existing != mark_price:
        raise TradingResearchError(f"inconsistent risk marks for instrument {identity!r}")
    marks[identity] = mark_price


def _add_delta(
    deltas: dict[InstrumentIdentity, Decimal],
    identity: InstrumentIdentity,
    quantity: Decimal,
) -> None:
    deltas[identity] = deltas.get(identity, Decimal(0)) + quantity


def _policy_has_execution_cost(policy: ExecutionPolicy) -> bool:
    return policy.slippage_bps != 0 or policy.fee_bps != 0 or policy.fixed_fee != 0


def _execution_reservation(
    intent: OrderIntent,
    mark_price: Decimal,
    policy: ExecutionPolicy,
    *,
    quantity: Decimal,
    include_fixed_fee: bool,
) -> _ExecutionReservation:
    fill_price = _worst_case_fill_price(intent, mark_price, policy)
    notional = quantity * fill_price
    fee = fee_for(notional, policy, include_fixed_fee=include_fixed_fee)

    if intent.side is Side.BUY:
        adverse_price_loss = max(Decimal(0), fill_price - mark_price) * quantity
        cash_required = notional + fee
    else:
        adverse_price_loss = max(Decimal(0), mark_price - fill_price) * quantity
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
    fee = fee_for(notional, policy, include_fixed_fee=False)
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


def _position_quantity(snapshot: AccountSnapshot, identity: InstrumentIdentity) -> Decimal:
    for position in snapshot.positions:
        if instrument_identity(position.instrument) == identity:
            return position.quantity
    return Decimal(0)
