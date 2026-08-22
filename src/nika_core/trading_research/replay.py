from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import IntEnum

from .accounting import PortfolioLedger
from .contracts import Bar, MarketEvent, Quote, TradingResearchError, require_aware_utc
from .dataset import event_sort_key
from .orders import (
    OrderState,
    OrderType,
    RiskApprovedOrder,
    Side,
    SimulatedFill,
    apply_slippage,
    fee_for,
)


class ReplayPhase(IntEnum):
    MARKET_DATA = 10
    EXISTING_ORDERS = 20
    ACCOUNTING = 30
    STRATEGY = 40
    RISK = 50
    QUEUE_NEW_ORDERS = 60


@dataclass(frozen=True, slots=True)
class TimeSlice:
    index: int
    at: datetime
    events: tuple[MarketEvent, ...]

    def __post_init__(self) -> None:
        if self.index < 0:
            raise TradingResearchError("slice index must be non-negative")
        at = require_aware_utc(self.at, "at")
        if any(event.time.available_at > at for event in self.events):
            raise TradingResearchError("time slice cannot contain future-unavailable market data")
        object.__setattr__(self, "at", at)
        object.__setattr__(self, "events", tuple(sorted(self.events, key=event_sort_key)))


@dataclass(frozen=True, slots=True)
class OrderUpdate:
    approval_id: str
    state: OrderState
    remaining_quantity: Decimal
    fill: SimulatedFill | None = None
    reason: str = ""


class SimulationExecutionEngine:
    """Deterministic paper-only execution; intentionally has no broker/send-order surface."""

    def execute(
        self,
        order: RiskApprovedOrder,
        time_slice: TimeSlice,
        *,
        remaining_quantity: Decimal | None = None,
    ) -> OrderUpdate:
        quantity = order.intent.quantity if remaining_quantity is None else remaining_quantity
        if quantity <= 0:
            raise TradingResearchError("remaining_quantity must be positive")
        if order.intent.expires_at is not None and time_slice.at >= order.intent.expires_at:
            return OrderUpdate(order.approval_id, OrderState.EXPIRED, quantity, reason="order expired")
        if time_slice.index <= order.intent.submitted_slice:
            return OrderUpdate(
                order.approval_id,
                OrderState.PENDING,
                quantity,
                reason="same-slice fill forbidden",
            )
        if time_slice.index <= order.approved_slice:
            return OrderUpdate(
                order.approval_id,
                OrderState.PENDING,
                quantity,
                reason="approval-slice fill forbidden",
            )
        if time_slice.at < order.active_at:
            return OrderUpdate(
                order.approval_id,
                OrderState.PENDING,
                quantity,
                reason="latency not elapsed",
            )

        market = self._market_for(order, time_slice)
        if market is None:
            return OrderUpdate(
                order.approval_id,
                OrderState.ACTIVE,
                quantity,
                reason="no executable market data",
            )
        price, available = market
        fill_quantity = min(quantity, available * order.policy.max_fill_fraction)
        if fill_quantity <= 0:
            return OrderUpdate(
                order.approval_id,
                OrderState.ACTIVE,
                quantity,
                reason="no modeled liquidity",
            )
        fill_price = apply_slippage(price, order.intent.side, order.policy.slippage_bps)
        notional = fill_quantity * fill_price
        fill = SimulatedFill(
            fill_id=f"fill:{order.approval_id}:{time_slice.index}:{fill_quantity}",
            approval_id=order.approval_id,
            intent_id=order.intent.intent_id,
            instrument=order.intent.instrument,
            side=order.intent.side,
            quantity=fill_quantity,
            price=fill_price,
            fee=fee_for(notional, order.policy),
            filled_at=time_slice.at,
            filled_slice=time_slice.index,
        )
        remaining = quantity - fill_quantity
        state = OrderState.FILLED if remaining == 0 else OrderState.PARTIALLY_FILLED
        return OrderUpdate(order.approval_id, state, remaining, fill=fill)

    def _market_for(
        self, order: RiskApprovedOrder, time_slice: TimeSlice
    ) -> tuple[Decimal, Decimal] | None:
        instrument_id = order.intent.instrument.instrument_id
        candidates = [
            event
            for event in time_slice.events
            if event.instrument.instrument_id == instrument_id
        ]
        if not candidates:
            return None
        event = candidates[-1]
        if isinstance(event, Quote):
            raw_price = event.ask if order.intent.side is Side.BUY else event.bid
            available = event.ask_size if order.intent.side is Side.BUY else event.bid_size
            if not self._limit_crosses(order, raw_price):
                return None
            return raw_price, available
        if isinstance(event, Bar):
            raw_price = event.open
            if order.intent.order_type is OrderType.LIMIT:
                limit = order.intent.limit_price
                assert limit is not None
                if order.intent.side is Side.BUY:
                    if event.low > limit:
                        return None
                    raw_price = min(event.open, limit)
                else:
                    if event.high < limit:
                        return None
                    raw_price = max(event.open, limit)
            return raw_price, event.volume
        return None

    @staticmethod
    def _limit_crosses(order: RiskApprovedOrder, market_price: Decimal) -> bool:
        if order.intent.order_type is OrderType.MARKET:
            return True
        limit = order.intent.limit_price
        assert limit is not None
        if order.intent.side is Side.BUY:
            return market_price <= limit
        return market_price >= limit


@dataclass(slots=True)
class ReplayBook:
    ledger: PortfolioLedger
    execution: SimulationExecutionEngine
    _remaining: dict[str, Decimal]
    _terminal: dict[str, OrderUpdate]

    def __init__(self, ledger: PortfolioLedger) -> None:
        self.ledger = ledger
        self.execution = SimulationExecutionEngine()
        self._remaining = {}
        self._terminal = {}

    def process_existing_order(self, order: RiskApprovedOrder, time_slice: TimeSlice) -> OrderUpdate:
        terminal = self._terminal.get(order.approval_id)
        if terminal is not None:
            return terminal
        remaining = self._remaining.get(order.approval_id, order.intent.quantity)
        update = self.execution.execute(order, time_slice, remaining_quantity=remaining)
        self._remaining[order.approval_id] = update.remaining_quantity
        if update.fill is not None:
            self.ledger.apply_fill(update.fill)
        if update.state in {OrderState.FILLED, OrderState.EXPIRED, OrderState.CANCELLED}:
            self._terminal[order.approval_id] = update
        return update

    def cancel(self, order: RiskApprovedOrder, reason: str = "cancelled by simulation") -> OrderUpdate:
        terminal = self._terminal.get(order.approval_id)
        if terminal is not None:
            return terminal
        remaining = self._remaining.get(order.approval_id, order.intent.quantity)
        update = OrderUpdate(order.approval_id, OrderState.CANCELLED, remaining, reason=reason)
        self._terminal[order.approval_id] = update
        return update
