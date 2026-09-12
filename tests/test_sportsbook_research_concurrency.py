from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal

from nika_core.data.sqlite import SQLiteStore
from nika_core.sportsbook_research import (
    Competition,
    Event,
    Market,
    OddsSnapshot,
    Participant,
    Selection,
    Sport,
    SportsbookCatalog,
    SportsbookSource,
)
from nika_core.sportsbook_research.repository import SQLiteSportsbookRepository
from nika_core.trading_research.contracts import EventTime


def _repository(tmp_path) -> SQLiteSportsbookRepository:
    store = SQLiteStore(tmp_path / "concurrent sportsbook.sqlite3")
    store.initialize()
    repository = SQLiteSportsbookRepository(store)
    repository.initialize()
    return repository


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
        selections=(
            Selection("a-win", "market", "A"),
            Selection("b-win", "market", "B"),
        ),
        sources=(SportsbookSource("source", "Source"),),
    )


def test_two_writers_exact_replay_produces_one_durable_insert(tmp_path) -> None:
    repository = _repository(tmp_path)
    repository.register_catalog(_catalog())
    time = EventTime(
        event_at=datetime(2026, 8, 27, 12, 1, tzinfo=UTC),
        source_at=datetime(2026, 8, 27, 12, 1, tzinfo=UTC),
        available_at=datetime(2026, 8, 27, 12, 1, tzinfo=UTC),
    )
    observation = OddsSnapshot(
        "source",
        "market",
        time,
        {"a-win": Decimal("1.90"), "b-win": Decimal("2.10")},
        1,
    )

    def write_once() -> bool:
        return _repository(tmp_path).ingest(observation)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: write_once(), range(2)))

    assert sorted(results) == [False, True]
    restarted = _repository(tmp_path)
    assert restarted.observations_at(time.available_at) == (observation,)
