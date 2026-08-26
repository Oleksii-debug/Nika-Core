from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.sportsbook_research import (
    Competition,
    Event,
    EventStatus,
    EventStatusCode,
    Market,
    Participant,
    Selection,
    Sport,
    SportsbookCatalog,
    SportsbookResearchError,
    SportsbookSource,
)
from nika_core.sportsbook_research.repository import SQLiteSportsbookRepository
from nika_core.trading_research.contracts import EventTime


def _repository(tmp_path) -> tuple[SQLiteSportsbookRepository, SQLiteStore]:
    store = SQLiteStore(tmp_path / "sportsbook query metadata integrity.sqlite3")
    store.initialize()
    repository = SQLiteSportsbookRepository(store)
    repository.initialize()
    return repository, store


def _catalog() -> SportsbookCatalog:
    return SportsbookCatalog(
        sports=(Sport("sport", "Sport"),),
        competitions=(Competition("competition", "sport", "Competition"),),
        participants=(Participant("a", "A"), Participant("b", "B")),
        events=(
            Event(
                "event",
                "competition",
                ("a", "b"),
                datetime(2026, 8, 27, 12, tzinfo=UTC),
            ),
        ),
        markets=(Market("market", "event", "Winner"),),
        selections=(Selection("a-win", "market", "A"),),
        sources=(SportsbookSource("source", "Source"),),
    )


def test_tampered_available_at_query_column_fails_closed(tmp_path) -> None:
    repository, store = _repository(tmp_path)
    repository.register_catalog(_catalog())

    event_at = datetime(2026, 8, 27, 12, 1, tzinfo=UTC)
    observation = EventStatus(
        "source",
        "event",
        EventTime(
            event_at=event_at,
            source_at=event_at,
            available_at=event_at + timedelta(seconds=30),
        ),
        EventStatusCode.LIVE,
        1,
    )
    repository.ingest(observation)

    # Simulate DB corruption/tamper that changes only the denormalized query column.
    # payload_json and payload_sha256 remain untouched and therefore still agree.
    with store.connection() as conn:
        conn.execute(
            "UPDATE sportsbook_observations SET available_at = ?",
            (event_at.isoformat(),),
        )

    decision_time = event_at + timedelta(seconds=10)
    with pytest.raises(SportsbookResearchError, match="integrity"):
        repository.observations_at(decision_time)
