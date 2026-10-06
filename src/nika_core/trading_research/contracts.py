from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class TradingResearchError(ValueError):
    """Base error for fail-closed trader research contracts."""


class FutureAccessError(TradingResearchError):
    """Raised when a caller attempts to consume information not yet available."""


class CausalityViolation(TradingResearchError):
    """Raised when a transform or feature lineage would leak future information."""


class Partition(str, Enum):
    TRAIN = "train"
    VALIDATION = "validation"
    TEST = "test"


@dataclass(frozen=True, slots=True)
class Venue:
    venue_id: str
    timezone: str = "UTC"

    def __post_init__(self) -> None:
        if not self.venue_id.strip():
            raise TradingResearchError("venue_id must not be empty")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise TradingResearchError(f"unknown IANA timezone: {self.timezone}") from exc


@dataclass(frozen=True, slots=True)
class Instrument:
    instrument_id: str
    venue: Venue
    currency: str

    def __post_init__(self) -> None:
        if not self.instrument_id.strip():
            raise TradingResearchError("instrument_id must not be empty")
        if len(self.currency) != 3 or not self.currency.isalpha():
            raise TradingResearchError("currency must be a three-letter code")
        object.__setattr__(self, "currency", self.currency.upper())


def require_aware_utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise TradingResearchError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class EventTime:
    event_at: datetime
    available_at: datetime
    source_at: datetime | None = None

    def __post_init__(self) -> None:
        event_at = require_aware_utc(self.event_at, "event_at")
        available_at = require_aware_utc(self.available_at, "available_at")
        source_at = (
            require_aware_utc(self.source_at, "source_at") if self.source_at is not None else None
        )
        if source_at is not None and available_at < source_at:
            raise TradingResearchError("available_at cannot precede source_at")
        object.__setattr__(self, "event_at", event_at)
        object.__setattr__(self, "available_at", available_at)
        object.__setattr__(self, "source_at", source_at)


@dataclass(frozen=True, slots=True)
class Bar:
    instrument: Instrument
    time: EventTime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    source_sequence: int = 0

    def __post_init__(self) -> None:
        if self.source_sequence < 0:
            raise TradingResearchError("source_sequence must be non-negative")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise TradingResearchError("bar prices must be positive")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise TradingResearchError("OHLC relationship is inconsistent")
        if self.low > self.high:
            raise TradingResearchError("bar low cannot exceed high")
        if self.volume < 0:
            raise TradingResearchError("bar volume cannot be negative")


@dataclass(frozen=True, slots=True)
class Tick:
    instrument: Instrument
    time: EventTime
    price: Decimal
    size: Decimal
    source_sequence: int = 0

    def __post_init__(self) -> None:
        if self.source_sequence < 0:
            raise TradingResearchError("source_sequence must be non-negative")
        if self.price <= 0 or self.size < 0:
            raise TradingResearchError("tick price must be positive and size non-negative")


@dataclass(frozen=True, slots=True)
class Quote:
    instrument: Instrument
    time: EventTime
    bid: Decimal
    ask: Decimal
    bid_size: Decimal = Decimal(0)
    ask_size: Decimal = Decimal(0)
    source_sequence: int = 0

    def __post_init__(self) -> None:
        if self.source_sequence < 0:
            raise TradingResearchError("source_sequence must be non-negative")
        if self.bid <= 0 or self.ask <= 0 or self.bid > self.ask:
            raise TradingResearchError("quote requires 0 < bid <= ask")
        if self.bid_size < 0 or self.ask_size < 0:
            raise TradingResearchError("quote sizes cannot be negative")


@dataclass(frozen=True, slots=True)
class OddsSnapshot:
    instrument: Instrument
    time: EventTime
    selections: Mapping[str, Decimal]
    source_sequence: int = 0

    def __post_init__(self) -> None:
        if self.source_sequence < 0:
            raise TradingResearchError("source_sequence must be non-negative")
        if not self.selections:
            raise TradingResearchError("odds snapshot must contain selections")
        copied = {str(key): Decimal(value) for key, value in self.selections.items()}
        if any(value <= 0 for value in copied.values()):
            raise TradingResearchError("odds must be positive")
        object.__setattr__(self, "selections", MappingProxyType(copied))


@dataclass(frozen=True, slots=True)
class OutcomeSettlement:
    instrument: Instrument
    time: EventTime
    outcome: str
    value: Decimal
    source_sequence: int = 0

    def __post_init__(self) -> None:
        if self.source_sequence < 0:
            raise TradingResearchError("source_sequence must be non-negative")
        if not self.outcome.strip():
            raise TradingResearchError("outcome must not be empty")


type MarketEvent = Bar | Tick | Quote | OddsSnapshot | OutcomeSettlement


@dataclass(frozen=True, slots=True)
class Provenance:
    source_id: str
    source_uri: str | None = None
    acquired_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    license_id: str | None = None

    def __post_init__(self) -> None:
        if not self.source_id.strip():
            raise TradingResearchError("source_id must not be empty")
        object.__setattr__(self, "acquired_at", require_aware_utc(self.acquired_at, "acquired_at"))
