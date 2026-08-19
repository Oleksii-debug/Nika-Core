from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import json
from typing import Iterable, Iterator, Protocol, Sequence

from .contracts import FutureAccessError, MarketEvent, Provenance, require_aware_utc


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: str
    message: str
    event_indexes: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class ValidationReport:
    duplicates: tuple[ValidationIssue, ...] = ()
    conflicts: tuple[ValidationIssue, ...] = ()
    gaps: tuple[ValidationIssue, ...] = ()

    @property
    def is_clean(self) -> bool:
        return not (self.duplicates or self.conflicts or self.gaps)


@dataclass(frozen=True, slots=True)
class DatasetVersion:
    dataset_id: str
    version: str
    raw_hash: str
    semantic_hash: str
    provenance: Provenance


def event_sort_key(event: MarketEvent) -> tuple[datetime, datetime, str, str, int]:
    return (
        event.time.available_at,
        event.time.event_at,
        event.instrument.venue.venue_id,
        event.instrument.instrument_id,
        event.source_sequence,
    )


def _jsonable(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "value") and isinstance(getattr(value, "value"), str):
        return getattr(value, "value")
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _jsonable(getattr(value, field.name)) for field in fields(value)}
    if hasattr(value, "items"):
        items = getattr(value, "items")()
        return {
            str(key): _jsonable(item)
            for key, item in sorted(items, key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def canonical_event_bytes(event: MarketEvent) -> bytes:
    payload = {"type": type(event).__name__, "value": _jsonable(event)}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def semantic_event_bytes(event: MarketEvent) -> bytes:
    payload = {
        "type": type(event).__name__,
        "instrument": event.instrument.instrument_id,
        "venue": event.instrument.venue.venue_id,
        "event_at": event.time.event_at.isoformat(),
        "available_at": event.time.available_at.isoformat(),
        "value": _jsonable(event),
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _digest(chunks: Iterable[bytes]) -> str:
    digest = sha256()
    for chunk in chunks:
        digest.update(len(chunk).to_bytes(8, "big"))
        digest.update(chunk)
    return digest.hexdigest()


class Dataset:
    __slots__ = ("_events", "version", "validation")

    def __init__(
        self,
        dataset_id: str,
        version: str,
        events: Sequence[MarketEvent],
        provenance: Provenance,
    ) -> None:
        ordered = tuple(sorted(events, key=event_sort_key))
        report = validate_events(ordered)
        raw_hash = _digest(canonical_event_bytes(event) for event in events)
        semantic_hash = _digest(semantic_event_bytes(event) for event in ordered)
        self._events = ordered
        self.validation = report
        self.version = DatasetVersion(dataset_id, version, raw_hash, semantic_hash, provenance)

    def temporal_view(self, at: datetime) -> "TemporalView":
        at = require_aware_utc(at, "at")
        visible = tuple(event for event in self._events if event.time.available_at <= at)
        return TemporalView._create(at, visible)

    def __len__(self) -> int:
        return len(self._events)


class TemporalView:
    __slots__ = ("_at", "_visible", "_trace_hash")
    _FORBIDDEN = frozenset(
        {"iloc", "loc", "index", "dataset", "events", "backing", "raw", "values"}
    )

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("TemporalView instances are created only by Dataset.temporal_view()")

    @classmethod
    def _create(cls, at: datetime, visible: tuple[MarketEvent, ...]) -> "TemporalView":
        obj = object.__new__(cls)
        obj._at = at
        obj._visible = visible
        obj._trace_hash = _digest(semantic_event_bytes(event) for event in visible)
        return obj

    @property
    def at(self) -> datetime:
        return self._at

    @property
    def trace_hash(self) -> str:
        return self._trace_hash

    def __len__(self) -> int:
        return len(self._visible)

    def __iter__(self) -> Iterator[MarketEvent]:
        return iter(self._visible)

    def __getitem__(self, index: int | slice) -> MarketEvent | tuple[MarketEvent, ...]:
        return self._visible[index]

    def __getattr__(self, name: str) -> object:
        if name in self._FORBIDDEN:
            raise FutureAccessError(f"TemporalView intentionally exposes no {name!r} backing access")
        raise AttributeError(name)

    def require_available(self, timestamp: datetime) -> None:
        timestamp = require_aware_utc(timestamp, "timestamp")
        if timestamp > self._at:
            raise FutureAccessError(
                "requested information is available only after the decision time: "
                f"{timestamp.isoformat()} > {self._at.isoformat()}"
            )


class DataProviderPort(Protocol):
    def dataset_version(self) -> DatasetVersion: ...

    def view_at(self, at: datetime) -> TemporalView: ...


class InMemoryDataProvider:
    __slots__ = ("_dataset",)

    def __init__(self, dataset: Dataset) -> None:
        self._dataset = dataset

    def dataset_version(self) -> DatasetVersion:
        return self._dataset.version

    def view_at(self, at: datetime) -> TemporalView:
        return self._dataset.temporal_view(at)


def _identity(event: MarketEvent) -> tuple[str, str, str, datetime, int]:
    return (
        type(event).__name__,
        event.instrument.venue.venue_id,
        event.instrument.instrument_id,
        event.time.event_at,
        event.source_sequence,
    )


def _gap_key(event: MarketEvent) -> tuple[str, str, str]:
    return (
        type(event).__name__,
        event.instrument.venue.venue_id,
        event.instrument.instrument_id,
    )


def validate_events(events: Sequence[MarketEvent]) -> ValidationReport:
    seen: dict[tuple[str, str, str, datetime, int], tuple[int, bytes]] = {}
    last_sequence: dict[tuple[str, str, str], tuple[int, int]] = {}
    duplicates: list[ValidationIssue] = []
    conflicts: list[ValidationIssue] = []
    gaps: list[ValidationIssue] = []
    for index, event in enumerate(events):
        identity = _identity(event)
        encoded = semantic_event_bytes(event)
        prior = seen.get(identity)
        if prior is None:
            seen[identity] = (index, encoded)
        elif prior[1] == encoded:
            duplicates.append(
                ValidationIssue("duplicate", "identical event identity and payload", (prior[0], index))
            )
        else:
            conflicts.append(
                ValidationIssue(
                    "conflict", "same event identity has different payload", (prior[0], index)
                )
            )

        if event.source_sequence > 0:
            key = _gap_key(event)
            previous = last_sequence.get(key)
            if previous is not None and event.source_sequence > previous[1] + 1:
                gaps.append(
                    ValidationIssue(
                        "sequence_gap",
                        f"source sequence jumps from {previous[1]} to {event.source_sequence}",
                        (previous[0], index),
                    )
                )
            if previous is None or event.source_sequence > previous[1]:
                last_sequence[key] = (index, event.source_sequence)
    return ValidationReport(tuple(duplicates), tuple(conflicts), tuple(gaps))
