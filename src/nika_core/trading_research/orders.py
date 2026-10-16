from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum

from .contracts import Instrument, TradingResearchError, require_aware_utc


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class OrderState(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class OrderIntent:
    intent_id: str
    instrument: Instrument
    side: Side
    order_type: OrderType
    quantity: Decimal
    submitted_at: datetime
    submitted_slice: int
    limit_price: Decimal | None = None
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.intent_id.strip():
            raise TradingResearchError("intent_id must not be empty")
        if self.quantity <= 0:
            raise TradingResearchError("order quantity must be positive")
        if type(self.submitted_slice) is not int or self.submitted_slice < 0:
            raise TradingResearchError("submitted_slice must be a non-negative integer")
        submitted_at = require_aware_utc(self.submitted_at, "submitted_at")
        expires_at = (
            require_aware_utc(self.expires_at, "expires_at") if self.expires_at is not None else None
        )
        if expires_at is not None and expires_at <= submitted_at:
            raise TradingResearchError("expires_at must be later than submitted_at")
        if self.order_type is OrderType.LIMIT:
            if self.limit_price is None or self.limit_price <= 0:
                raise TradingResearchError("limit orders require a positive limit_price")
        elif self.limit_price is not None:
            raise TradingResearchError("market orders cannot carry limit_price")
        object.__setattr__(self, "expires_at", expires_at)
        object.__setattr__(self, "submitted_at", submitted_at)


@dataclass(frozen=True, slots=True)
class OrderAuthority:
    """Host-stamped execution identity; strategy proposal metadata is not authority."""

    workspace_id: str
    run_id: str
    order_id: str
    submitted_at: datetime
    submitted_slice: int

    def __post_init__(self) -> None:
        for field_name in ("workspace_id", "run_id", "order_id"):
            value = getattr(self, field_name)
            if type(value) is not str or not value.strip():
                raise TradingResearchError(f"{field_name} must be nonblank text")
        if type(self.submitted_slice) is not int or self.submitted_slice < 0:
            raise TradingResearchError("authority submitted_slice must be non-negative")
        object.__setattr__(
            self,
            "submitted_at",
            require_aware_utc(self.submitted_at, "authority submitted_at"),
        )


def order_authority_sha256(authority: OrderAuthority) -> str:
    if type(authority) is not OrderAuthority:
        raise TradingResearchError("order authority must be an OrderAuthority")
    payload = json.dumps(
        (
            authority.workspace_id,
            authority.run_id,
            authority.order_id,
            authority.submitted_at.isoformat(),
            authority.submitted_slice,
        ),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    policy_id: str
    latency: timedelta = timedelta(0)
    slippage_bps: Decimal = Decimal(0)
    fee_bps: Decimal = Decimal(0)
    fixed_fee: Decimal = Decimal(0)
    max_fill_fraction: Decimal = Decimal(1)

    def __post_init__(self) -> None:
        if not self.policy_id.strip():
            raise TradingResearchError("policy_id must not be empty")
        if self.latency < timedelta(0):
            raise TradingResearchError("latency cannot be negative")
        if self.slippage_bps < 0 or self.fee_bps < 0 or self.fixed_fee < 0:
            raise TradingResearchError("execution costs cannot be negative")
        if self.max_fill_fraction <= 0 or self.max_fill_fraction > 1:
            raise TradingResearchError("max_fill_fraction must be in (0, 1]")


@dataclass(frozen=True, slots=True)
class RiskApprovedOrder:
    approval_id: str
    intent: OrderIntent
    authority: OrderAuthority
    approved_at: datetime
    approved_slice: int
    policy: ExecutionPolicy

    def __post_init__(self) -> None:
        if not self.approval_id.strip():
            raise TradingResearchError("approval_id must not be empty")
        if type(self.authority) is not OrderAuthority:
            raise TradingResearchError("approved order requires host OrderAuthority")
        approved_at = require_aware_utc(self.approved_at, "approved_at")
        if type(self.approved_slice) is not int or self.approved_slice < 0:
            raise TradingResearchError("approved_slice must be a non-negative integer")
        if self.approved_slice < self.authority.submitted_slice:
            raise TradingResearchError("approval cannot precede authority slice")
        if approved_at < self.authority.submitted_at:
            raise TradingResearchError("approval cannot precede authority time")
        if (
            self.intent.expires_at is not None
            and self.intent.expires_at <= self.authority.submitted_at
        ):
            raise TradingResearchError("intent expiry must be later than host submission")
        object.__setattr__(self, "approved_at", approved_at)

    @property
    def active_at(self) -> datetime:
        return (self.approved_at + self.policy.latency).astimezone(UTC)


@dataclass(frozen=True, slots=True)
class SimulatedFill:
    fill_id: str
    approval_id: str
    intent_id: str
    authority: OrderAuthority
    instrument: Instrument
    side: Side
    quantity: Decimal
    price: Decimal
    fee: Decimal
    filled_at: datetime
    filled_slice: int

    def __post_init__(self) -> None:
        if not self.fill_id.strip():
            raise TradingResearchError("fill_id must not be empty")
        if type(self.authority) is not OrderAuthority:
            raise TradingResearchError("simulated fill requires host OrderAuthority")
        if self.quantity <= 0 or self.price <= 0 or self.fee < 0:
            raise TradingResearchError("fill quantity/price must be positive and fee non-negative")
        if type(self.filled_slice) is not int:
            raise TradingResearchError("filled_slice must be an integer")
        if self.filled_slice <= self.authority.submitted_slice:
            raise TradingResearchError("fill must occur after host submission slice")
        filled_at = require_aware_utc(self.filled_at, "filled_at")
        if filled_at < self.authority.submitted_at:
            raise TradingResearchError("fill cannot precede host submission time")
        object.__setattr__(self, "filled_at", filled_at)


def fee_for(
    notional: Decimal,
    policy: ExecutionPolicy,
    *,
    include_fixed_fee: bool = True,
) -> Decimal:
    """Return deterministic fee; fixed_fee is charged once per approved order."""

    if notional < 0:
        raise TradingResearchError("notional cannot be negative")
    fixed = policy.fixed_fee if include_fixed_fee else Decimal(0)
    return fixed + (notional * policy.fee_bps / Decimal(10_000))


def apply_slippage(price: Decimal, side: Side, bps: Decimal) -> Decimal:
    if price <= 0 or bps < 0:
        raise TradingResearchError("price must be positive and slippage non-negative")
    multiplier = Decimal(1) + Decimal(side.sign) * bps / Decimal(10_000)
    result = price * multiplier
    if result <= 0:
        raise TradingResearchError("slippage produced non-positive price")
    return result
