from datetime import UTC, datetime
from decimal import Decimal

import pytest

from nika_core.trading_research import Bar, Dataset, EventTime, Instrument, Provenance, Venue

NOW = datetime(2026, 1, 1, tzinfo=UTC)
VENUE = Venue("xnas", "America/New_York")
INSTRUMENT = Instrument("MSFT", VENUE, "usd")
PROVENANCE = Provenance("fixture", license_id="test-only", acquired_at=NOW)


def make(close: str, sequence: int = 1) -> Bar:
    return Bar(
        INSTRUMENT,
        EventTime(NOW, NOW, NOW),
        Decimal("100"),
        Decimal("110"),
        Decimal("90"),
        Decimal(close),
        Decimal("1"),
        sequence,
    )


def test_duplicate_and_conflict_classification() -> None:
    duplicate = Dataset("d", "1", [make("100"), make("100")], PROVENANCE)
    assert len(duplicate.validation.duplicates) == 1
    assert not duplicate.validation.conflicts
    conflict = Dataset("d", "1", [make("100"), make("101")], PROVENANCE)
    assert len(conflict.validation.conflicts) == 1
    assert not conflict.validation.duplicates


def test_raw_hash_keeps_input_order_while_semantic_hash_is_order_stable() -> None:
    first = make("100", 1)
    second = make("101", 2)
    a = Dataset("d", "1", [first, second], PROVENANCE)
    b = Dataset("d", "1", [second, first], PROVENANCE)
    assert a.version.raw_hash != b.version.raw_hash
    assert a.version.semantic_hash == b.version.semantic_hash


def test_timezone_rules_normalize_to_utc_and_reject_naive() -> None:
    assert EventTime(NOW, NOW).event_at.tzinfo is UTC
    with pytest.raises(ValueError, match="timezone-aware"):
        EventTime(datetime(2026, 1, 1), NOW)


def test_invalid_iana_timezone_fails_closed() -> None:
    with pytest.raises(ValueError, match="IANA timezone"):
        Venue("bad", "Mars/Olympus")
