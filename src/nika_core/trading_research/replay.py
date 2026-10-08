from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, fields
from datetime import datetime, timezone, timedelta
from types import MappingProxyType
from zoneinfo import ZoneInfo
from decimal import Decimal
from enum import IntEnum
from gc import get_referents

from .accounting import PortfolioLedger
from .contracts import (
    Bar,
    EventTime,
    Instrument,
    MarketEvent,
    OddsSnapshot,
    OutcomeSettlement,
    Quote,
    Tick,
    TradingResearchError,
    Venue,
    require_aware_utc,
)
from .dataset import canonical_event_bytes, event_sort_key
from .identity import InstrumentIdentity, instrument_identity, instrument_identity_sha256
from .risk import _validate_policy
from .orders import (
    OrderIntent,
    OrderState,
    OrderType,
    OrderAuthority,
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
        if type(self.index) is not int or self.index < 0:
            raise TradingResearchError("slice index must be a non-negative integer")
        at = require_aware_utc(self.at, "at")
        if any(event.time.available_at > at for event in self.events):
            raise TradingResearchError("time slice cannot contain future-unavailable market data")
        ordered = tuple(sorted(self.events, key=event_sort_key))
        _validate_same_slice_chronology(ordered)
        object.__setattr__(self, "at", at)
        object.__setattr__(self, "events", ordered)



 
def _require_inert_paper_carriers(root: object) -> None:
    """Reject behavioral nested inputs before deepcopy or market-data iteration.

    These records are already typed by canonical domain constructors, but a
    frozen dataclass may have its fields replaced with object.__setattr__ after
    construction. Do not run attacker-controlled __deepcopy__, numeric methods
    or custom mapping methods while re-admitting a paper order/slice.
    """
    records = (
        TimeSlice, Venue, Instrument, EventTime, Bar, Tick, Quote,
        OddsSnapshot, OutcomeSettlement, OrderIntent, OrderAuthority,
        ExecutionPolicy, RiskApprovedOrder,
    )
    pending = [root]
    seen: set[int] = set()
    while pending:
        value = pending.pop()
        kind = type(value)
        if kind in (str, int, bool, Side, OrderType, type(None)):
            continue
        if kind is Decimal:
            if not value.is_finite():
                raise TradingResearchError("non-finite paper carrier decimal")
            continue
        if kind is datetime:
            # datetime is a builtin, but its tzinfo can be arbitrary Python
            # code (including __deepcopy__). Permit standard fixed/IANA zones.
            if type(value.tzinfo) not in (timezone, ZoneInfo):
                raise TradingResearchError("unsupported paper carrier timezone")
            continue
        if kind is timedelta:
            continue
        identity = id(value)
        if identity in seen:
            continue
        seen.add(identity)
        if len(seen) > 100_000:
            raise TradingResearchError("paper carrier exceeds bounded size")
        if kind is tuple:
            pending.extend(value)
        elif kind in (dict, MappingProxyType):
            # A mappingproxy may wrap an arbitrary behavior-bearing Mapping.
            # On CPython, inspect the referent without calling its methods;
            # fail closed if the implementation cannot prove an exact dict.
            if kind is MappingProxyType:
                referents = get_referents(value)
                if len(referents) != 1 or type(referents[0]) is not dict:
                    raise TradingResearchError("behavioral paper odds mapping is forbidden")
            for key, item in value.items():
                if type(key) is not str or type(item) is not Decimal:
                    raise TradingResearchError("invalid paper odds selection carrier")
                pending.append(item)
        elif kind in records:
            pending.extend(getattr(value, field.name) for field in fields(kind))
        else:
            raise TradingResearchError("behavioral paper carrier is forbidden")


def _snapshot_validated_time_slice(time_slice: TimeSlice) -> TimeSlice:
    """Detach and re-admit a market slice at the last paper-execution boundary.

    Frozen data classes can be modified with object.__setattr__, so validity at
    initial construction cannot grant perpetual authorization to consume data.
    Rebuild each event (including mapping-backed odds) before validation: the
    replay and its evidence must read one detached version of the same slice.
    """
    if type(time_slice) is not TimeSlice or type(time_slice.events) is not tuple:
        raise TradingResearchError("paper replay requires a canonical time slice")
    _require_inert_paper_carriers(time_slice)
    detached: list[MarketEvent] = []
    for event in time_slice.events:
        if type(event) is OddsSnapshot:
            copied = OddsSnapshot(
                deepcopy(event.instrument),
                deepcopy(event.time),
                dict(event.selections),
                event.source_sequence,
            )
        elif type(event) in (Bar, Tick, Quote, OutcomeSettlement):
            copied = deepcopy(event)
        else:
            raise TradingResearchError("unsupported paper market event")
        if type(copied.time) is not EventTime:
            raise TradingResearchError("paper market event requires EventTime")
        copied.time.__post_init__()
        copied.__post_init__()
        detached.append(copied)
    admitted = TimeSlice(time_slice.index, time_slice.at, tuple(detached))
    if admitted != time_slice:
        raise TradingResearchError("unstable paper market slice identity")
    return admitted


def _snapshot_validated_approved_order(order: RiskApprovedOrder) -> RiskApprovedOrder:
    """Re-admit mutable frozen carriers before a paper execution/cancel effect.

    Approval provenance stays with the canonical RiskEngine and host authority;
    replay may not silently accept structurally invalid post-approval mutation.
    """
    if type(order) is not RiskApprovedOrder:
        raise TradingResearchError("paper replay requires a risk-approved order")
    _require_inert_paper_carriers(order)
    detached = deepcopy(order)
    if (
        type(detached.intent) is not OrderIntent
        or type(detached.authority) is not OrderAuthority
        or type(detached.intent.instrument) is not Instrument
        or type(detached.intent.instrument.venue) is not Venue
        or type(detached.intent.side) is not Side
        or type(detached.intent.order_type) is not OrderType
        or type(detached.intent.quantity) is not Decimal
        or not detached.intent.quantity.is_finite()
    ):
        raise TradingResearchError("invalid paper approved order carrier")
    detached.intent.instrument.venue.__post_init__()
    detached.intent.instrument.__post_init__()
    detached.intent.__post_init__()
    detached.authority.__post_init__()
    _validate_policy(detached.policy)
    detached.__post_init__()
    if detached != order:
        raise TradingResearchError("unstable paper approved order identity")
    return detached


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
        time_slice = _snapshot_validated_time_slice(time_slice)
        order = _snapshot_validated_approved_order(order)
        quantity = order.intent.quantity if remaining_quantity is None else remaining_quantity
        if quantity <= 0:
            raise TradingResearchError("remaining_quantity must be positive")
        if order.intent.expires_at is not None and time_slice.at >= order.intent.expires_at:
            return OrderUpdate(order.approval_id, OrderState.EXPIRED, quantity, reason="order expired")
        if time_slice.index <= order.authority.submitted_slice:
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
            authority=order.authority,
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


type ReplayOrderKey = tuple[str, str, str, str, InstrumentIdentity]


def _replay_order_key(order: RiskApprovedOrder) -> ReplayOrderKey:
    return (
        order.authority.workspace_id,
        order.authority.run_id,
        order.authority.order_id,
        order.approval_id,
        instrument_identity(order.intent.instrument),
    )


@dataclass(slots=True)
class ReplayBook:
    ledger: PortfolioLedger
    execution: SimulationExecutionEngine
    _remaining: dict[ReplayOrderKey, Decimal]
    _terminal: dict[ReplayOrderKey, OrderUpdate]
    _last_slice: dict[ReplayOrderKey, tuple[int, datetime, tuple[bytes, ...], OrderUpdate]]
    _accepted_orders: dict[ReplayOrderKey, RiskApprovedOrder]

    def __init__(self, ledger: PortfolioLedger) -> None:
        self.ledger = ledger
        self.execution = SimulationExecutionEngine()
        self._remaining = {}
        self._terminal = {}
        self._last_slice = {}
        self._accepted_orders = {}

    def process_existing_order(self, order: RiskApprovedOrder, time_slice: TimeSlice) -> OrderUpdate:
        order = _snapshot_validated_approved_order(order)
        key = _replay_order_key(order)
        # A reused approval identity cannot change intent, policy or authority.
        accepted = self._accepted_orders.get(key)
        if accepted is not None and order != accepted:
            raise TradingResearchError("conflicting approved order replay identity")
        # Prepare the detached identity before accounting/cancellation effects.
        # Even an unexpected copy failure cannot leave a partial transition.
        order_snapshot = accepted if accepted is not None else deepcopy(order)
        if order != order_snapshot:
            raise TradingResearchError("unstable approved order replay identity")
        terminal = self._terminal.get(key)
        if terminal is not None:
            return terminal
        # Each order may consume a market slice at most once. Without a
        # per-order slice fence a repeated partial fill uses the same fill ID:
        # PortfolioLedger deduplicates it but _remaining would still shrink,
        # creating phantom execution quantity. Snapshot event bytes so mutable
        # caller-held event objects cannot rewrite the replay identity.
        time_slice = _snapshot_validated_time_slice(time_slice)
        slice_events = tuple(canonical_event_bytes(event) for event in time_slice.events)
        previous = self._last_slice.get(key)
        if previous is not None:
            previous_index, previous_at, previous_events, previous_update = previous
            if time_slice.index < previous_index:
                raise TradingResearchError("order replay slice cannot move backwards")
            if time_slice.index > previous_index and time_slice.at < previous_at:
                raise TradingResearchError("order replay time cannot move backwards")
            if time_slice.index == previous_index:
                if time_slice.at != previous_at or slice_events != previous_events:
                    raise TradingResearchError("conflicting same-slice order replay")
                return previous_update
        remaining = self._remaining.get(key, order.intent.quantity)
        update = self.execution.execute(order_snapshot, time_slice, remaining_quantity=remaining)
        # Accounting must succeed before advancing order replay state. A failed
        # account admission must leave this order replayable after recovery.
        if update.fill is not None:
            self.ledger.apply_fill(update.fill)
        self._remaining[key] = update.remaining_quantity
        self._last_slice[key] = (time_slice.index, time_slice.at, slice_events, update)
        self._accepted_orders.setdefault(key, order_snapshot)
        if update.state in {OrderState.FILLED, OrderState.EXPIRED, OrderState.CANCELLED}:
            self._terminal[key] = update
        return update

    def cancel(self, order: RiskApprovedOrder, reason: str = "cancelled by simulation") -> OrderUpdate:
        order = _snapshot_validated_approved_order(order)
        key = _replay_order_key(order)
        # A reused approval identity cannot change intent, policy or authority.
        accepted = self._accepted_orders.get(key)
        if accepted is not None and order != accepted:
            raise TradingResearchError("conflicting approved order replay identity")
        # Prepare the detached identity before accounting/cancellation effects.
        # Even an unexpected copy failure cannot leave a partial transition.
        order_snapshot = accepted if accepted is not None else deepcopy(order)
        if order != order_snapshot:
            raise TradingResearchError("unstable approved order replay identity")
        terminal = self._terminal.get(key)
        if terminal is not None:
            return terminal
        remaining = self._remaining.get(key, order.intent.quantity)
        update = OrderUpdate(order.approval_id, OrderState.CANCELLED, remaining, reason=reason)
        self._terminal[key] = update
        self._accepted_orders.setdefault(key, order_snapshot)
        return update
