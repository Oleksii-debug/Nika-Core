from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import IntEnum

from .accounting import PortfolioLedger
from .contracts import MarketEvent, Quote, TradingResearchError, require_aware_utc
from .dataset import canonical_event_bytes, event_sort_key
from .identity import InstrumentIdentity, instrument_identity, instrument_identity_sha256
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
        ordered = tuple(sorted(self.events, key=event_sort_key))
        _validate_same_slice_chronology(ordered)
        object.__setattr__(self, "at", at)
        object.__setattr__(self, "events", ordered)


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
        fill_price = _legal_fill_price(order, price)
        notional = fill_quantity * fill_price
        first_fill = quantity == order.intent.quantity
        fill = SimulatedFill(
            fill_id=(
                f"fill:{order.approval_id}:"
                f"{instrument_identity_sha256(order.intent.instrument)}:"
                f"{time_slice.index}:{fill_quantity}"
            ),
            approval_id=order.approval_id,
            intent_id=order.intent.intent_id,
            instrument=order.intent.instrument,
            side=order.intent.side,
            quantity=fill_quantity,
            price=fill_price,
            fee=fee_for(notional, order.policy, include_fixed_fee=first_fill),
            filled_at=time_slice.at,
            filled_slice=time_slice.index,
        )
        remaining = quantity - fill_quantity
        state = OrderState.FILLED if remaining == 0 else OrderState.PARTIALLY_FILLED
        return OrderUpdate(order.approval_id, state, remaining, fill=fill)

    def _market_for(
        self, order: RiskApprovedOrder, time_slice: TimeSlice
    ) -> tuple[Decimal, Decimal] | None:
        identity = instrument_identity(order.intent.instrument)
        candidates = [
            event
            for event in time_slice.events
            if isinstance(event, Quote) and instrument_identity(event.instrument) == identity
        ]
        if not candidates:
            return None
        event = candidates[-1]
        raw_price = event.ask if order.intent.side is Side.BUY else event.bid
        available = event.ask_size if order.intent.side is Side.BUY else event.bid_size
        if not self._limit_crosses(order, raw_price):
            return None
        return raw_price, available

    @staticmethod
    def _limit_crosses(order: RiskApprovedOrder, market_price: Decimal) -> bool:
        if order.intent.order_type is OrderType.MARKET:
            return True
        limit = order.intent.limit_price
        assert limit is not None
        if order.intent.side is Side.BUY:
            return market_price <= limit
        return market_price >= limit



def _validate_same_slice_chronology(events: tuple[MarketEvent, ...]) -> None:
    seen: dict[tuple[InstrumentIdentity, datetime, datetime, int], bytes] = {}
    for event in events:
        key = (
            instrument_identity(event.instrument),
            event.time.available_at,
            event.time.event_at,
            event.source_sequence,
        )
        payload = canonical_event_bytes(event)
        previous = seen.get(key)
        if previous is not None and previous != payload:
            raise TradingResearchError("ambiguous same-slice market chronology")
        seen[key] = payload


def _legal_fill_price(order: RiskApprovedOrder, market_price: Decimal) -> Decimal:
    slipped = apply_slippage(market_price, order.intent.side, order.policy.slippage_bps)
    if order.intent.order_type is OrderType.MARKET:
        return slipped
    limit = order.intent.limit_price
    if limit is None:
        raise TradingResearchError("limit order missing limit_price")
    if order.intent.side is Side.BUY:
        return min(slipped, limit)
    return max(slipped, limit)


def _replay_order_key(order: RiskApprovedOrder) -> tuple[str, InstrumentIdentity]:
    return order.approval_id, instrument_identity(order.intent.instrument)


@dataclass(slots=True)
class ReplayBook:
    ledger: PortfolioLedger
    execution: SimulationExecutionEngine
    _remaining: dict[tuple[str, InstrumentIdentity], Decimal]
    _terminal: dict[tuple[str, InstrumentIdentity], OrderUpdate]

    def __init__(self, ledger: PortfolioLedger) -> None:
        self.ledger = ledger
        self.execution = SimulationExecutionEngine()
        self._remaining = {}
        self._terminal = {}

    def process_existing_order(self, order: RiskApprovedOrder, time_slice: TimeSlice) -> OrderUpdate:
        key = _replay_order_key(order)
        terminal = self._terminal.get(key)
        if terminal is not None:
            return terminal
        remaining = self._remaining.get(key, order.intent.quantity)
        update = self.execution.execute(order, time_slice, remaining_quantity=remaining)
        self._remaining[key] = update.remaining_quantity
        if update.fill is not None:
            self.ledger.apply_fill(update.fill)
        if update.state in {OrderState.FILLED, OrderState.EXPIRED, OrderState.CANCELLED}:
            self._terminal[key] = update
        return update

    def cancel(self, order: RiskApprovedOrder, reason: str = "cancelled by simulation") -> OrderUpdate:
        key = _replay_order_key(order)
        terminal = self._terminal.get(key)
        if terminal is not None:
            return terminal
        remaining = self._remaining.get(key, order.intent.quantity)
        update = OrderUpdate(order.approval_id, OrderState.CANCELLED, remaining, reason=reason)
        self._terminal[key] = update
        return update
