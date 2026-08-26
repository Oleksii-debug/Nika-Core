from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum


class SportsbookValidationError(ValueError):
    """Raised when provider data violates deterministic domain invariants."""


def _normalized_id(value: str, field: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise SportsbookValidationError(f"{field} must not be empty")
    if "\x00" in normalized:
        raise SportsbookValidationError(f"{field} must not contain NUL")
    if len(normalized) > 256:
        raise SportsbookValidationError(f"{field} must be at most 256 characters")
    return normalized


def _utc(value: datetime, field: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SportsbookValidationError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class CausalTime:
    """Leakage-safe three-clock timeline: event <= source <= locally available."""

    event_at: datetime
    source_at: datetime
    available_at: datetime

    def __post_init__(self) -> None:
        event_at = _utc(self.event_at, "event_at")
        source_at = _utc(self.source_at, "source_at")
        available_at = _utc(self.available_at, "available_at")
        if event_at > source_at:
            raise SportsbookValidationError("event_at must not be later than source_at")
        if source_at > available_at:
            raise SportsbookValidationError("source_at must not be later than available_at")
        object.__setattr__(self, "event_at", event_at)
        object.__setattr__(self, "source_at", source_at)
        object.__setattr__(self, "available_at", available_at)


class EventStatus(StrEnum):
    SCHEDULED = "scheduled"
    LIVE = "live"
    SUSPENDED = "suspended"
    FINISHED = "finished"
    CANCELLED = "cancelled"
    POSTPONED = "postponed"


class MarketStatus(StrEnum):
    OPEN = "open"
    SUSPENDED = "suspended"
    CLOSED = "closed"
    SETTLED = "settled"


class SettlementOutcome(StrEnum):
    WON = "won"
    LOST = "lost"
    VOID = "void"
    PUSH = "push"


@dataclass(frozen=True, slots=True)
class SportsbookSource:
    source_id: str
    name: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", _normalized_id(self.source_id, "source_id"))
        if not self.name.strip():
            raise SportsbookValidationError("source name must not be empty")


@dataclass(frozen=True, slots=True)
class Competition:
    competition_id: str
    sport: str
    name: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "competition_id", _normalized_id(self.competition_id, "competition_id")
        )
        if not self.sport.strip() or not self.name.strip():
            raise SportsbookValidationError("competition sport and name must not be empty")


@dataclass(frozen=True, slots=True)
class Participant:
    participant_id: str
    name: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "participant_id", _normalized_id(self.participant_id, "participant_id")
        )
        if not self.name.strip():
            raise SportsbookValidationError("participant name must not be empty")


@dataclass(frozen=True, slots=True)
class Event:
    event_id: str
    competition_id: str
    participant_ids: tuple[str, ...]
    starts_at: datetime
    status: EventStatus = EventStatus.SCHEDULED

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _normalized_id(self.event_id, "event_id"))
        object.__setattr__(
            self, "competition_id", _normalized_id(self.competition_id, "competition_id")
        )
        normalized_participants = tuple(
            _normalized_id(value, "participant_id") for value in self.participant_ids
        )
        if not normalized_participants:
            raise SportsbookValidationError("event must have at least one participant")
        if len(set(normalized_participants)) != len(normalized_participants):
            raise SportsbookValidationError("event participant_ids must be unique")
        object.__setattr__(self, "participant_ids", normalized_participants)
        object.__setattr__(self, "starts_at", _utc(self.starts_at, "starts_at"))


@dataclass(frozen=True, slots=True)
class Market:
    market_id: str
    event_id: str
    key: str
    name: str
    status: MarketStatus = MarketStatus.OPEN

    def __post_init__(self) -> None:
        object.__setattr__(self, "market_id", _normalized_id(self.market_id, "market_id"))
        object.__setattr__(self, "event_id", _normalized_id(self.event_id, "event_id"))
        if not self.key.strip() or not self.name.strip():
            raise SportsbookValidationError("market key and name must not be empty")


@dataclass(frozen=True, slots=True)
class Selection:
    selection_id: str
    market_id: str
    label: str
    participant_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "selection_id", _normalized_id(self.selection_id, "selection_id")
        )
        object.__setattr__(self, "market_id", _normalized_id(self.market_id, "market_id"))
        if not self.label.strip():
            raise SportsbookValidationError("selection label must not be empty")
        if self.participant_id is not None:
            object.__setattr__(
                self,
                "participant_id",
                _normalized_id(self.participant_id, "participant_id"),
            )


@dataclass(frozen=True, slots=True)
class OddsSnapshot:
    snapshot_id: str
    source_id: str
    selection_id: str
    decimal_odds: Decimal
    timeline: CausalTime

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "snapshot_id", _normalized_id(self.snapshot_id, "snapshot_id")
        )
        object.__setattr__(self, "source_id", _normalized_id(self.source_id, "source_id"))
        object.__setattr__(
            self, "selection_id", _normalized_id(self.selection_id, "selection_id")
        )
        odds = Decimal(self.decimal_odds)
        if not odds.is_finite() or odds <= Decimal("1"):
            raise SportsbookValidationError("decimal_odds must be finite and greater than 1")
        object.__setattr__(self, "decimal_odds", odds)


@dataclass(frozen=True, slots=True)
class ScoreValue:
    participant_id: str
    value: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "participant_id", _normalized_id(self.participant_id, "participant_id")
        )
        value = Decimal(self.value)
        if not value.is_finite() or value < 0:
            raise SportsbookValidationError("score value must be finite and non-negative")
        object.__setattr__(self, "value", value)


@dataclass(frozen=True, slots=True)
class ScoreState:
    score_id: str
    source_id: str
    event_id: str
    values: tuple[ScoreValue, ...]
    timeline: CausalTime

    def __post_init__(self) -> None:
        object.__setattr__(self, "score_id", _normalized_id(self.score_id, "score_id"))
        object.__setattr__(self, "source_id", _normalized_id(self.source_id, "source_id"))
        object.__setattr__(self, "event_id", _normalized_id(self.event_id, "event_id"))
        if not self.values:
            raise SportsbookValidationError("score state must contain at least one value")
        ids = [value.participant_id for value in self.values]
        if len(set(ids)) != len(ids):
            raise SportsbookValidationError("score participant_ids must be unique")


@dataclass(frozen=True, slots=True)
class PeriodState:
    period_state_id: str
    source_id: str
    event_id: str
    period: str
    clock: str | None
    status: EventStatus
    timeline: CausalTime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "period_state_id",
            _normalized_id(self.period_state_id, "period_state_id"),
        )
        object.__setattr__(self, "source_id", _normalized_id(self.source_id, "source_id"))
        object.__setattr__(self, "event_id", _normalized_id(self.event_id, "event_id"))
        if not self.period.strip():
            raise SportsbookValidationError("period must not be empty")
        if self.clock is not None and not self.clock.strip():
            raise SportsbookValidationError("clock must be None or non-empty")


@dataclass(frozen=True, slots=True)
class Settlement:
    settlement_id: str
    source_id: str
    selection_id: str
    outcome: SettlementOutcome
    timeline: CausalTime

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "settlement_id", _normalized_id(self.settlement_id, "settlement_id")
        )
        object.__setattr__(self, "source_id", _normalized_id(self.source_id, "source_id"))
        object.__setattr__(
            self, "selection_id", _normalized_id(self.selection_id, "selection_id")
        )
