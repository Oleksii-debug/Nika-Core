from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.table_tennis import (
    IngestDisposition,
    MatchObservation,
    PlayerRef,
    TableTennisIntegrityError,
    TableTennisRepository,
)


def _match() -> MatchObservation:
    played = datetime(2026, 8, 3, 10, tzinfo=UTC)
    return MatchObservation(
        source_id="research:wtt",
        source_record_id="unicode-матч",
        source_revision=1,
        source_locator="https://example.invalid/wtt/unicode-match",
        source_evidence_sha256="b" * 64,
        observed_at=played + timedelta(minutes=20),
        played_at=played,
        event_name="Kyiv Open",
        player_a=PlayerRef("a", "Олена"),
        player_b=PlayerRef("b", "Ірина"),
        sets_a=3,
        sets_b=2,
    )


def test_concurrent_exact_replays_converge_to_one_revision(tmp_path) -> None:
    db_path = tmp_path / "parallel stats.sqlite3"
    observation = _match()

    def ingest_once() -> IngestDisposition:
        repository = TableTennisRepository(SQLiteStore(db_path))
        return repository.ingest(observation).disposition

    with ThreadPoolExecutor(max_workers=6) as pool:
        dispositions = list(pool.map(lambda _: ingest_once(), range(12)))

    assert dispositions.count(IngestDisposition.INSERTED) == 1
    assert dispositions.count(IngestDisposition.REPLAYED) == 11
    assert TableTennisRepository(SQLiteStore(db_path)).revision_count() == 1


def test_payload_tamper_fails_closed(tmp_path) -> None:
    db_path = tmp_path / "stats.sqlite3"
    repository = TableTennisRepository(SQLiteStore(db_path))
    repository.ingest(_match())

    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE table_tennis_matches SET sets_a = 9")
        conn.commit()

    with pytest.raises(TableTennisIntegrityError, match="payload hash mismatch"):
        repository.list_current_matches()


def test_head_hash_tamper_fails_closed(tmp_path) -> None:
    db_path = tmp_path / "stats.sqlite3"
    repository = TableTennisRepository(SQLiteStore(db_path))
    repository.ingest(_match())

    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE table_tennis_match_heads SET latest_payload_sha256 = ?", ("0" * 64,))
        conn.commit()

    with pytest.raises(TableTennisIntegrityError, match="head payload hash mismatch"):
        repository.list_current_matches()


def test_missing_head_is_detected_before_new_ingest(tmp_path) -> None:
    db_path = tmp_path / "stats.sqlite3"
    repository = TableTennisRepository(SQLiteStore(db_path))
    repository.ingest(_match())

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DELETE FROM table_tennis_match_heads")
        conn.commit()

    with pytest.raises(TableTennisIntegrityError, match="without a durable head"):
        repository.ingest(_match())
