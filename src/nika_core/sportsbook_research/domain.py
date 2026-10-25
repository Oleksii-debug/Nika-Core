from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from types import MappingProxyType
from urllib.parse import parse_qsl, urlsplit

from nika_core.trading_research.contracts import EventTime, require_aware_utc


class SportsbookResearchError(ValueError):
    """Base error for fail-closed read-only sportsbook research contracts."""


class SportsbookConflictError(SportsbookResearchError):
    """Raised when one durable identity is replayed with different semantic bytes."""


def _required(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise SportsbookResearchError(f"{field_name} must not be empty")
    return normalized


def _unique_ids(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    normalized = tuple(_required(value, field_name) for value in values)
    if len(set(normalized)) != len(normalized):
        raise SportsbookResearchError(f"{field_name} must be unique")
    return normalized


def _finite_decimal(value: Decimal | int | str, field_name: str) -> Decimal:
    try:
        result = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise SportsbookResearchError(f"{field_name} must be a decimal number") from exc
    if not result.is_finite():
        raise SportsbookResearchError(f"{field_name} must be finite")
    return result


def _source_sequence(value: int) -> int:
    if type(value) is not int or value < 0:
        raise SportsbookResearchError("source_sequence must be a non-negative integer")
    return value


def _safe_source_uri(value: str) -> str:
    normalized = _required(value, "source_uri")
    parsed = urlsplit(normalized)
    if parsed.username is not None or parsed.password is not None:
        raise SportsbookResearchError("source_uri must not contain credentials")
    sensitive_query_keys = {
        "access_token",
        "api_key",
        "api_token",
        "authorization",
        "cookie",
        "credential",
        "credentials",
        "password",
        "refresh_token",
        "secret",
        "session",
        "session_id",
        "token",
    }
    for key, _value in parse_qsl(parsed.query, keep_blank_values=True):
        normalized_key = key.strip().lower().replace("-", "_")
        if normalized_key in sensitive_query_keys:
            raise SportsbookResearchError(
                f"source_uri must not contain credential query parameter: {key}"
            )
    return normalized


@dataclass(frozen=True, slots=True)
class Sport:
    sport_id: str
    name: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "sport_id", _required(self.sport_id, "sport_id"))
        object.__setattr__(self, "name", _required(self.name, "name"))


@dataclass(frozen=True, slots=True)
class Competition:
    competition_id: str
    sport_id: str
    name: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "competition_id",
            _required(self.competition_id, "competition_id"),
        )
        object.__setattr__(self, "sport_id", _required(self.sport_id, "sport_id"))
        object.__setattr__(self, "name", _required(self.name, "name"))


@dataclass(frozen=True, slots=True)
class Participant:
    participant_id: str
    name: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "participant_id",
            _required(self.participant_id, "participant_id"),
        )
        object.__setattr__(self, "name", _required(self.name, "name"))


@dataclass(frozen=True, slots=True)
class Event:
    event_id: str
    competition_id: str
    participant_ids: tuple[str, ...]
    scheduled_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _required(self.event_id, "event_id"))
        object.__setattr__(
            self,
            "competition_id",
            _required(self.competition_id, "competition_id"),
        )
        participants = _unique_ids(tuple(self.participant_ids), "participant_id")
        if not participants:
            raise SportsbookResearchError("event must contain at least one participant")
        object.__setattr__(self, "participant_ids", participants)
        object.__setattr__(
            self,
            "scheduled_at",
            require_aware_utc(self.scheduled_at, "scheduled_at"),
        )


@dataclass(frozen=True, slots=True)
class Market:
    market_id: str
    event_id: str
    name: str
    period_key: str = "full"

    def __post_init__(self) -> None:
        object.__setattr__(self, "market_id", _required(self.market_id, "market_id"))
        object.__setattr__(self, "event_id", _required(self.event_id, "event_id"))
        object.__setattr__(self, "name", _required(self.name, "name"))
        object.__setattr__(self, "period_key", _required(self.period_key, "period_key"))


@dataclass(frozen=True, slots=True)
class Selection:
    selection_id: str
    market_id: str
    name: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "selection_id",
            _required(self.selection_id, "selection_id"),
        )
        object.__setattr__(self, "market_id", _required(self.market_id, "market_id"))
        object.__setattr__(self, "name", _required(self.name, "name"))


@dataclass(frozen=True, slots=True)
class SportsbookSource:
    source_id: str
    name: str
    source_uri: str | None = None
    license_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", _required(self.source_id, "source_id"))
        object.__setattr__(self, "name", _required(self.name, "name"))
        if self.source_uri is not None:
            object.__setattr__(self, "source_uri", _safe_source_uri(self.source_uri))
        if self.license_id is not None:
            object.__setattr__(self, "license_id", _required(self.license_id, "license_id"))


@dataclass(frozen=True, slots=True)
class SportsbookCatalog:
    sports: tuple[Sport, ...]
    competitions: tuple[Competition, ...]
    participants: tuple[Participant, ...]
    events: tuple[Event, ...]
    markets: tuple[Market, ...]
    selections: tuple[Selection, ...]
    sources: tuple[SportsbookSource, ...]

    def __post_init__(self) -> None:
        sports = {item.sport_id: item for item in self.sports}
        competitions = {item.competition_id: item for item in self.competitions}
        participants = {item.participant_id: item for item in self.participants}
        events = {item.event_id: item for item in self.events}
        markets = {item.market_id: item for item in self.markets}
        selections = {item.selection_id: item for item in self.selections}
        sources = {item.source_id: item for item in self.sources}
        collections = (
            ("sport_id", self.sports, sports),
            ("competition_id", self.competitions, competitions),
            ("participant_id", self.participants, participants),
            ("event_id", self.events, events),
            ("market_id", self.markets, markets),
            ("selection_id", self.selections, selections),
            ("source_id", self.sources, sources),
        )
        for field_name, values, indexed in collections:
            if len(values) != len(indexed):
                raise SportsbookResearchError(f"duplicate {field_name} in catalog")
        for competition in self.competitions:
            if competition.sport_id not in sports:
                raise SportsbookResearchError(
                    f"competition references unknown sport: {competition.sport_id}"
                )
        for event in self.events:
            if event.competition_id not in competitions:
                raise SportsbookResearchError(
                    f"event references unknown competition: {event.competition_id}"
                )
            unknown = [item for item in event.participant_ids if item not in participants]
            if unknown:
                raise SportsbookResearchError(
                    f"event references unknown participant: {unknown[0]}"
                )
        for market in self.markets:
            if market.event_id not in events:
                raise SportsbookResearchError(
                    f"market references unknown event: {market.event_id}"
                )
        selection_counts = {market_id: 0 for market_id in markets}
        for selection in self.selections:
            if selection.market_id not in markets:
                raise SportsbookResearchError(
                    f"selection references unknown market: {selection.market_id}"
                )
            selection_counts[selection.market_id] += 1
        empty_market = next(
            (market_id for market_id, count in selection_counts.items() if count == 0),
            None,
        )
        if empty_market is not None:
            raise SportsbookResearchError(
                f"market must contain at least one selection: {empty_market}"
            )
        object.__setattr__(self, "sports", tuple(sorted(self.sports, key=lambda item: item.sport_id)))
        object.__setattr__(
            self,
            "competitions",
            tuple(sorted(self.competitions, key=lambda item: item.competition_id)),
        )
        object.__setattr__(
            self,
            "participants",
            tuple(sorted(self.participants, key=lambda item: item.participant_id)),
        )
        object.__setattr__(self, "events", tuple(sorted(self.events, key=lambda item: item.event_id)))
        object.__setattr__(
            self,
            "markets",
            tuple(sorted(self.markets, key=lambda item: item.market_id)),
        )
        object.__setattr__(
            self,
            "selections",
            tuple(sorted(self.selections, key=lambda item: item.selection_id)),
        )
        object.__setattr__(
            self,
            "sources",
            tuple(sorted(self.sources, key=lambda item: item.source_id)),
        )


class EventStatusCode(str, Enum):
    SCHEDULED = "scheduled"
    LIVE = "live"
    SUSPENDED = "suspended"
    FINAL = "final"
    CANCELLED = "cancelled"
    POSTPONED = "postponed"


def _copy_int_mapping(values: Mapping[str, int], field_name: str) -> Mapping[str, int]:
    copied: dict[str, int] = {}
    for key, value in values.items():
        normalized = _required(str(key), field_name)
        if normalized in copied:
            raise SportsbookResearchError(f"duplicate normalized {field_name} key: {normalized}")
        if type(value) is not int or value < 0:
            raise SportsbookResearchError(
                f"{field_name} values must be non-negative integers"
            )
        copied[normalized] = value
    if not copied:
        raise SportsbookResearchError(f"{field_name} must not be empty")
    return MappingProxyType(copied)


def _copy_decimal_mapping(
    values: Mapping[str, Decimal | int | str],
    field_name: str,
    *,
    minimum_exclusive: Decimal | None = None,
    minimum_inclusive: Decimal | None = None,
    maximum_inclusive: Decimal | None = None,
) -> Mapping[str, Decimal]:
    copied: dict[str, Decimal] = {}
    for key, value in values.items():
        normalized = _required(str(key), field_name)
        if normalized in copied:
            raise SportsbookResearchError(f"duplicate normalized {field_name} key: {normalized}")
        parsed = _finite_decimal(value, field_name)
        if minimum_exclusive is not None and parsed <= minimum_exclusive:
            raise SportsbookResearchError(
                f"{field_name} values must be greater than {minimum_exclusive}"
            )
        if minimum_inclusive is not None and parsed < minimum_inclusive:
            raise SportsbookResearchError(
                f"{field_name} values must be at least {minimum_inclusive}"
            )
        if maximum_inclusive is not None and parsed > maximum_inclusive:
            raise SportsbookResearchError(
                f"{field_name} values must be at most {maximum_inclusive}"
            )
        copied[normalized] = parsed
    if not copied:
        raise SportsbookResearchError(f"{field_name} must not be empty")
    return MappingProxyType(copied)


@dataclass(frozen=True, slots=True)
class OddsSnapshot:
    source_id: str
    market_id: str
    time: EventTime
    prices: Mapping[str, Decimal]
    source_sequence: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", _required(self.source_id, "source_id"))
        object.__setattr__(self, "market_id", _required(self.market_id, "market_id"))
        object.__setattr__(
            self,
            "prices",
            _copy_decimal_mapping(
                self.prices,
                "odds",
                minimum_exclusive=Decimal("1"),
            ),
        )
        object.__setattr__(self, "source_sequence", _source_sequence(self.source_sequence))


@dataclass(frozen=True, slots=True)
class ScoreState:
    source_id: str
    event_id: str
    time: EventTime
    scores: Mapping[str, int]
    source_sequence: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", _required(self.source_id, "source_id"))
        object.__setattr__(self, "event_id", _required(self.event_id, "event_id"))
        object.__setattr__(self, "scores", _copy_int_mapping(self.scores, "scores"))
        object.__setattr__(self, "source_sequence", _source_sequence(self.source_sequence))


@dataclass(frozen=True, slots=True)
class PeriodState:
    source_id: str
    event_id: str
    time: EventTime
    period_key: str
    ordinal: int
    clock_seconds_remaining: Decimal | None = None
    source_sequence: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", _required(self.source_id, "source_id"))
        object.__setattr__(self, "event_id", _required(self.event_id, "event_id"))
        object.__setattr__(self, "period_key", _required(self.period_key, "period_key"))
        if type(self.ordinal) is not int or self.ordinal < 0:
            raise SportsbookResearchError("ordinal must be a non-negative integer")
        if self.clock_seconds_remaining is not None:
            value = _finite_decimal(
                self.clock_seconds_remaining,
                "clock_seconds_remaining",
            )
            if value < 0:
                raise SportsbookResearchError(
                    "clock_seconds_remaining must not be negative"
                )
            object.__setattr__(self, "clock_seconds_remaining", value)
        object.__setattr__(self, "source_sequence", _source_sequence(self.source_sequence))


@dataclass(frozen=True, slots=True)
class EventStatus:
    source_id: str
    event_id: str
    time: EventTime
    status: EventStatusCode
    source_sequence: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", _required(self.source_id, "source_id"))
        object.__setattr__(self, "event_id", _required(self.event_id, "event_id"))
        if not isinstance(self.status, EventStatusCode):
            try:
                object.__setattr__(self, "status", EventStatusCode(self.status))
            except ValueError as exc:
                raise SportsbookResearchError("unknown event status") from exc
        object.__setattr__(self, "source_sequence", _source_sequence(self.source_sequence))


@dataclass(frozen=True, slots=True)
class Settlement:
    source_id: str
    market_id: str
    time: EventTime
    outcomes: Mapping[str, Decimal]
    source_sequence: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_id", _required(self.source_id, "source_id"))
        object.__setattr__(self, "market_id", _required(self.market_id, "market_id"))
        object.__setattr__(
            self,
            "outcomes",
            _copy_decimal_mapping(
                self.outcomes,
                "settlement",
                minimum_inclusive=Decimal("0"),
                maximum_inclusive=Decimal("1"),
            ),
        )
        object.__setattr__(self, "source_sequence", _source_sequence(self.source_sequence))


type SportsbookObservation = OddsSnapshot | ScoreState | PeriodState | EventStatus | Settlement
