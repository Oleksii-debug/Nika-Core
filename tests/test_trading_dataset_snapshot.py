from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from decimal import Decimal

from nika_core.trading_research import (
    Bar,
    Dataset,
    EventTime,
    Instrument,
    Provenance,
    Venue,
)

BASE = datetime(2026, 1, 1, tzinfo=UTC)


class ChangingSequence(Sequence[Bar]):
    """A source whose later iterations present a different valid event."""

    def __init__(self, first: Bar, later: Bar) -> None:
        self.first = first
        self.later = later
        self.iterations = 0

    def __len__(self) -> int:
        return 1

    def __getitem__(self, index: int | slice) -> Bar | tuple[Bar, ...]:
        if isinstance(index, slice):
            return (self.first,)[index]
        return (self.first,)[index]

    def __iter__(self) -> Iterator[Bar]:
        self.iterations += 1
        return iter((self.first,) if self.iterations == 1 else (self.later,))


def _bar(instrument: Instrument, close: int) -> Bar:
    price = Decimal(close)
    return Bar(
        instrument,
        EventTime(BASE, BASE),
        price,
        price + 1,
        price - 1,
        price,
        Decimal(10),
    )


def test_dataset_raw_hash_validation_and_view_use_same_captured_source() -> None:
    instrument = Instrument("ABC", Venue("test", "UTC"), "usd")
    original = _bar(instrument, 10)
    altered = _bar(instrument, 20)
    provenance = Provenance("fixture", acquired_at=BASE)
    changing = ChangingSequence(original, altered)

    result = Dataset("stable", "1", changing, provenance)
    expected = Dataset("stable", "1", (original,), provenance)

    assert changing.iterations == 1
    assert result.version.raw_hash == expected.version.raw_hash
    assert result.version.semantic_hash == expected.version.semantic_hash
    assert result.validation == expected.validation
    assert tuple(result.temporal_view(BASE)) == (original,)
