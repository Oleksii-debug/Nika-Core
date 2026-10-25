from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.sportsbook_research import (
    Competition,
    Event,
    EventStatus,
    EventStatusCode,
    Market,
    OddsSnapshot,
    Participant,
    PeriodState,
    ScoreState,
    Selection,
    Settlement,
    Sport,
    SportsbookCatalog,
    SportsbookConflictError,
    SportsbookResearchError,
    SportsbookSource,
)
from nika_core.sportsbook_research.ports import SportsbookSourcePort
from nika_core.sportsbook_research.repository import SQLiteSportsbookRepository
from nika_core.trading_research.contracts import EventTime


def _catalog() -> SportsbookCatalog:
    return SportsbookCatalog(
        sports=(Sport("table-tennis", "Настільний теніс"),),
        competitions=(Competition("league-a", "table-tennis", "Ліга А"),),
        participants=(
            Participant("alice", "Alice"),
            Participant("bob", "Bob"),
        ),
        events=(
            Event(
                "event-1",
                "league-a",
                ("alice", "bob"),
                datetime(2026, 8, 27, 16, tzinfo=UTC),
            ),
        ),
        markets=(Market("match-winner", "event-1", "Match winner"),),
        selections=(
            Selection("alice-win", "match-winner", "Alice"),
            Selection("bob-win", "match-winner", "Bob"),
        ),
        sources=(
            SportsbookSource(
                "licensed-feed",
                "Licensed feed",
                "https://example.invalid/feed",
                "LicenseRef-1",
            ),
        ),
    )


def _time(*, minute: int, available_delay: int = 0) -> EventTime:
    event_at = datetime(2026, 8, 27, 16, minute, tzinfo=UTC)
    return EventTime(
        event_at=event_at,
        source_at=event_at,
        available_at=event_at + timedelta(seconds=available_delay),
    )


def _repo(tmp_path) -> SQLiteSportsbookRepository:
    store = SQLiteStore(tmp_path / "дані sportsbook" / "ніка research.sqlite3")
    store.initialize()
    repository = SQLiteSportsbookRepository(store)
    repository.initialize()
    return repository


def test_catalog_roundtrip_and_unicode_path_restart(tmp_path) -> None:
    repository = _repo(tmp_path)
    catalog = _catalog()

    assert repository.register_catalog(catalog) == 9
    assert repository.register_catalog(catalog) == 0

    restarted = _repo(tmp_path)
    assert restarted.load_catalog() == catalog


def test_catalog_identity_is_immutable(tmp_path) -> None:
    repository = _repo(tmp_path)
    repository.register_catalog(_catalog())
    changed = _catalog()
    changed = SportsbookCatalog(
        sports=(Sport("table-tennis", "Different name"),),
        competitions=changed.competitions,
        participants=changed.participants,
        events=changed.events,
        markets=changed.markets,
        selections=changed.selections,
        sources=changed.sources,
    )

    with pytest.raises(SportsbookConflictError):
        repository.register_catalog(changed)

    assert repository.load_catalog() == _catalog()


def test_odds_dedup_and_conflicting_replay_fail_closed(tmp_path) -> None:
    repository = _repo(tmp_path)
    repository.register_catalog(_catalog())
    original = OddsSnapshot(
        "licensed-feed",
        "match-winner",
        _time(minute=1, available_delay=2),
        {"alice-win": Decimal("1.91"), "bob-win": Decimal("2.05")},
        7,
    )

    assert repository.ingest(original) is True
    assert repository.ingest(original) is False

    conflicting = OddsSnapshot(
        "licensed-feed",
        "match-winner",
        original.time,
        {"alice-win": Decimal("1.95"), "bob-win": Decimal("2.05")},
        7,
    )
    with pytest.raises(SportsbookConflictError):
        repository.ingest(conflicting)

    assert repository.observations_at(original.time.available_at) == (original,)


def test_temporal_query_uses_available_at_not_event_at(tmp_path) -> None:
    repository = _repo(tmp_path)
    repository.register_catalog(_catalog())
    delayed = EventStatus(
        "licensed-feed",
        "event-1",
        _time(minute=2, available_delay=30),
        EventStatusCode.LIVE,
        8,
    )
    repository.ingest(delayed)

    decision_time = delayed.time.event_at + timedelta(seconds=10)
    assert repository.observations_at(decision_time) == ()
    assert repository.observations_at(delayed.time.available_at) == (delayed,)


def test_batch_is_atomic_when_later_observation_conflicts(tmp_path) -> None:
    repository = _repo(tmp_path)
    repository.register_catalog(_catalog())
    original = OddsSnapshot(
        "licensed-feed",
        "match-winner",
        _time(minute=3, available_delay=1),
        {"alice-win": Decimal("1.80"), "bob-win": Decimal("2.20")},
        9,
    )
    repository.ingest(original)
    new_status = EventStatus(
        "licensed-feed",
        "event-1",
        _time(minute=4, available_delay=1),
        EventStatusCode.LIVE,
        10,
    )
    conflicting = OddsSnapshot(
        original.source_id,
        original.market_id,
        original.time,
        {"alice-win": Decimal("1.81"), "bob-win": Decimal("2.20")},
        original.source_sequence,
    )

    with pytest.raises(SportsbookConflictError):
        repository.ingest_many((new_status, conflicting))

    at = new_status.time.available_at + timedelta(seconds=1)
    assert repository.observations_at(at) == (original,)


def test_all_observation_types_roundtrip_after_restart(tmp_path) -> None:
    repository = _repo(tmp_path)
    repository.register_catalog(_catalog())
    observations = (
        ScoreState(
            "licensed-feed",
            "event-1",
            _time(minute=5),
            {"alice": 2, "bob": 1},
            11,
        ),
        PeriodState(
            "licensed-feed",
            "event-1",
            _time(minute=6),
            "game-4",
            4,
            Decimal("42.5"),
            12,
        ),
        EventStatus(
            "licensed-feed",
            "event-1",
            _time(minute=7),
            EventStatusCode.FINAL,
            13,
        ),
        Settlement(
            "licensed-feed",
            "match-winner",
            _time(minute=8),
            {"alice-win": Decimal("1"), "bob-win": Decimal("0")},
            14,
        ),
    )
    assert repository.ingest_many(observations) == 4

    restarted = _repo(tmp_path)
    at = observations[-1].time.available_at + timedelta(seconds=1)
    assert restarted.observations_at(at, event_id="event-1") == observations


def test_unknown_source_selection_and_score_participant_fail_closed(tmp_path) -> None:
    repository = _repo(tmp_path)
    repository.register_catalog(_catalog())

    with pytest.raises(SportsbookResearchError, match="unknown source"):
        repository.ingest(
            EventStatus("unknown", "event-1", _time(minute=9), EventStatusCode.LIVE, 15)
        )
    with pytest.raises(SportsbookResearchError, match="unknown selection"):
        repository.ingest(
            OddsSnapshot(
                "licensed-feed",
                "match-winner",
                _time(minute=10),
                {"unknown-selection": Decimal("2.00")},
                16,
            )
        )
    with pytest.raises(SportsbookResearchError, match="outside event"):
        repository.ingest(
            ScoreState(
                "licensed-feed",
                "event-1",
                _time(minute=11),
                {"charlie": 1},
                17,
            )
        )


def test_future_schema_and_corrupt_payload_fail_closed(tmp_path) -> None:
    repository = _repo(tmp_path)
    repository.register_catalog(_catalog())
    store = SQLiteStore(tmp_path / "дані sportsbook" / "ніка research.sqlite3")

    with store.connection() as conn:
        conn.execute(
            "UPDATE sportsbook_entities SET payload_json = ? "
            "WHERE entity_type = 'sport' AND entity_id = 'table-tennis'",
            ('{"sport_id":"table-tennis","name":"tampered"}',),
        )
    with pytest.raises(SportsbookResearchError, match="integrity"):
        repository.load_catalog()

    broken_store = SQLiteStore(tmp_path / "broken-history.sqlite3")
    broken_store.initialize()
    with broken_store.connection() as conn:
        conn.execute(
            "CREATE TABLE sportsbook_schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO sportsbook_schema_migrations(version, applied_at) VALUES (2, ?)",
            (datetime.now(UTC).isoformat(),),
        )
    with pytest.raises(SportsbookResearchError, match="not contiguous"):
        SQLiteSportsbookRepository(broken_store).initialize()

    future_store = SQLiteStore(tmp_path / "future.sqlite3")
    future_store.initialize()
    with future_store.connection() as conn:
        conn.execute(
            "CREATE TABLE sportsbook_schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        timestamp = datetime.now(UTC).isoformat()
        conn.executemany(
            "INSERT INTO sportsbook_schema_migrations(version, applied_at) VALUES (?, ?)",
            ((1, timestamp), (2, timestamp)),
        )
    with pytest.raises(SportsbookResearchError, match="newer"):
        SQLiteSportsbookRepository(future_store).initialize()


def test_provider_port_exposes_read_only_surface() -> None:
    names = set(vars(SportsbookSourcePort))
    assert {"source", "read_catalog", "read_observations"}.issubset(names)
    assert not names.intersection(
        {"place_bet", "wager", "deposit", "withdraw", "fund", "redeem_credential"}
    )


def test_domain_rejects_non_finite_or_non_decimal_odds() -> None:
    with pytest.raises(SportsbookResearchError, match="finite"):
        OddsSnapshot(
            "licensed-feed",
            "match-winner",
            _time(minute=12),
            {"alice-win": Decimal("NaN")},
        )
    with pytest.raises(SportsbookResearchError, match="greater"):
        OddsSnapshot(
            "licensed-feed",
            "match-winner",
            _time(minute=12),
            {"alice-win": Decimal("1")},
        )


def test_observation_rows_are_queryable_without_secret_material(tmp_path) -> None:
    repository = _repo(tmp_path)
    repository.register_catalog(_catalog())
    repository.ingest(
        EventStatus(
            "licensed-feed",
            "event-1",
            _time(minute=13),
            EventStatusCode.LIVE,
            18,
        )
    )
    db_path = tmp_path / "дані sportsbook" / "ніка research.sqlite3"
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT source_id, event_id, payload_json FROM sportsbook_observations"
        ).fetchone()
    assert row is not None
    assert row[0:2] == ("licensed-feed", "event-1")
    assert "credential" not in row[2].lower()
    assert "token" not in row[2].lower()
