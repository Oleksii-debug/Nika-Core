from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from .accounting import AccountSnapshot
from .contracts import TradingResearchError, require_aware_utc
from .orders import ExecutionPolicy, OrderIntent, RiskApprovedOrder


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
    ) -> RiskApprovedOrder:
        approved_at = require_aware_utc(approved_at, "approved_at")
        if mark_price <= 0:
            raise TradingResearchError("mark_price must be positive")
        current_qty = _position_quantity(snapshot, intent.instrument.instrument_id)
        signed = intent.quantity * Decimal(intent.side.sign)
        projected_qty = current_qty + pending_signed_quantity + signed
        if not self._limits.allow_short and projected_qty < 0:
            raise RiskRejected("short positions are disabled")
        if abs(projected_qty) > self._limits.max_abs_position:
            raise RiskRejected("max_abs_position exceeded")

        current_value = current_qty * mark_price
        projected_value = projected_qty * mark_price
        projected_net = snapshot.net_exposure - current_value + projected_value
        projected_gross = snapshot.gross_exposure - abs(current_value) + abs(projected_value)
        if projected_gross > self._limits.max_gross_exposure:
            raise RiskRejected("max_gross_exposure exceeded")
        if abs(projected_net) > self._limits.max_net_exposure:
            raise RiskRejected("max_net_exposure exceeded")
        if snapshot.equity <= 0 and projected_gross > 0:
            raise RiskRejected("positive equity required for exposure")
        if snapshot.equity > 0 and projected_gross / snapshot.equity > self._limits.max_leverage:
            raise RiskRejected("max_leverage exceeded")

        session_loss = max(Decimal(0), risk_state.session_start_equity - snapshot.equity)
        drawdown = max(Decimal(0), risk_state.peak_equity - snapshot.equity)
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


def _position_quantity(snapshot: AccountSnapshot, instrument_id: str) -> Decimal:
    for position in snapshot.positions:
        if position.instrument.instrument_id == instrument_id:
            return position.quantity
    return Decimal(0)
