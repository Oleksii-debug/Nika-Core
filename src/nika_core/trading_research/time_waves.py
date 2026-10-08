"""Causal, presentation-neutral event time waves for the existing Trader workspace.

This is an immutable read projection over Dataset.temporal_view(), not a new
scheduler, trading runtime, order executor, portfolio store or data provider.
Market events can be in the future (prematch) only if their data is available
at the decision time. Each result contains detached primitive identities.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from .contracts import (
    Bar,
    EventTime,
    FutureAccessError,
    Instrument,
    OddsSnapshot,
    OutcomeSettlement,
    Quote,
    Tick,
    TradingResearchError,
    Venue,
)
from .dataset import TemporalView
from .identity import InstrumentIdentity, instrument_identity

_MARKET_EVENT_TYPES = (Bar, Tick, Quote, OddsSnapshot, OutcomeSettlement)
_MAX_VISIBLE_EVENTS = 100_000


@dataclass(frozen=True, slots=True)
class TimeWave:
    """Deterministic accessible-report value with no authority or live objects."""

    start_at: datetime
    end_at: datetime
    event_count: int
    instruments: tuple[InstrumentIdentity, ...]


def _admit_event(event: object, at: datetime) -> tuple[datetime, InstrumentIdentity]:
    """Reject behavioral or post-decision carriers before reading any identifiers."""
    if type(event) not in _MARKET_EVENT_TYPES:
        raise TradingResearchError("invalid time-wave market event")
    if (
        type(event.time) is not EventTime
        or type(event.instrument) is not Instrument
        or type(event.instrument.venue) is not Venue
        or type(event.source_sequence) is not int
        or event.source_sequence < 0
    ):
        raise TradingResearchError("invalid time-wave market identity")
    time = event.time
    if (
        type(time.event_at) is not datetime
        or type(time.available_at) is not datetime
        or time.event_at.tzinfo is not UTC
        or time.available_at.tzinfo is not UTC
    ):
        raise TradingResearchError("invalid time-wave timestamps")
    # Frozen market carriers can be mutated after temporal visibility admission.
    # A source observation must not follow its advertised availability time.
    source_at = time.source_at
    if source_at is not None and (
        type(source_at) is not datetime
        or source_at.tzinfo is not UTC
        or source_at > time.available_at
    ):
        raise TradingResearchError("invalid time-wave source-time provenance")
    if time.available_at > at:
        raise FutureAccessError("time-wave event not yet available at decision time")
    identity = instrument_identity(event.instrument)
    if any(
        type(part) is not str
        or not part
        or part != part.strip()
        or not part.isprintable()
        or len(part.encode("utf-8")) > 512
        for part in identity
    ):
        raise TradingResearchError("invalid time-wave instrument identity")
    return time.event_at, identity


def group_visible_time_waves(
    view: TemporalView, *, window_minutes: int = 60, max_events: int = _MAX_VISIBLE_EVENTS
) -> tuple[TimeWave, ...]:
    """Group only available events into UTC event-time windows for paper reports.

    Historical and prematch windows use EVENT time, never publish/available time.
    The exact canonical TemporalView controls visibility. A future event may
    appear as upcoming only when its quote/data was available before 'view.at'.
    This function does not request hidden events, mutate the view or place orders.
    """
    if type(view) is not TemporalView:
        raise TypeError("canonical temporal view is required")
    if (
        type(window_minutes) is not int
        or window_minutes < 1
        or window_minutes > 1440
        or 1440 % window_minutes != 0
    ):
        raise TradingResearchError("invalid time-wave window size")
    if type(max_events) is not int or not 1 <= max_events <= _MAX_VISIBLE_EVENTS:
        raise TradingResearchError("invalid time-wave event budget")
    if len(view) > max_events:
        raise TradingResearchError("time-wave visible event budget exceeded")
    at = view.at
    if type(at) is not datetime or at.tzinfo is not UTC:
        raise TradingResearchError("invalid decision timestamp")

    counts: dict[datetime, int] = {}
    instruments: dict[datetime, set[InstrumentIdentity]] = {}
    ends: dict[datetime, datetime] = {}
    for event in view:
        event_at, identity = _admit_event(event, at)
        minute = (event_at.hour * 60 + event_at.minute) // window_minutes * window_minutes
        start = event_at.replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            start += timedelta(minutes=minute)
            end = start + timedelta(minutes=window_minutes)
        except OverflowError as exc:
            raise TradingResearchError("time-wave date exceeds supported range") from exc
        ends[start] = end
        counts[start] = counts.get(start, 0) + 1
        instruments.setdefault(start, set()).add(identity)

    return tuple(
        TimeWave(
            start_at=start,
            end_at=ends[start],
            event_count=counts[start],
            instruments=tuple(sorted(instruments[start])),
        )
        for start in sorted(counts)
    )
