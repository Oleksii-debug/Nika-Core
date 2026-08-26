from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nika_core.sportsbook_research import (
    OddsSnapshot,
    PeriodState,
    Settlement,
    SportsbookResearchError,
)
from nika_core.trading_research.contracts import EventTime


def _time() -> EventTime:
    at = datetime(2026, 8, 27, 12, tzinfo=UTC)
    return EventTime(event_at=at, source_at=at, available_at=at)


def test_float_odds_are_rejected_instead_of_binary_coerced_to_decimal() -> None:
    with pytest.raises(SportsbookResearchError, match="decimal"):
        OddsSnapshot(
            "source",
            "market",
            _time(),
            {"selection": 2.1},  # type: ignore[dict-item]
        )


def test_float_settlement_values_are_rejected_instead_of_binary_coerced() -> None:
    with pytest.raises(SportsbookResearchError, match="decimal"):
        Settlement(
            "source",
            "market",
            _time(),
            {"selection": 0.5},  # type: ignore[dict-item]
        )


def test_float_period_clock_is_rejected_instead_of_binary_coerced() -> None:
    with pytest.raises(SportsbookResearchError, match="decimal"):
        PeriodState(
            "source",
            "event",
            _time(),
            "first_half",
            1,
            1.5,  # type: ignore[arg-type]
        )
