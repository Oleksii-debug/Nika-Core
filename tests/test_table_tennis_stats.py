from __future__ import annotations

import csv
import io
from datetime import UTC, datetime, timedelta

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.table_tennis import (
    IngestDisposition,
    MatchObservation,
    PlayerRef,
    TableTennisRepository,
    TableTennisRevisionError,
    TableTennisStatsService,
    TableTennisValidationError,
    render_csv_report,
    render_text_report,
)


def _match(
    *,
    record_id: str = "m-1",
    revision: int = 1,
    sets_a: int = 3,
    sets_b: int = 1,
    a_id: str = "player-a",
    a_name: str = "Аліна",
    b_id: str = "player-b",
    b_name: str = "Béla",
    played_at: datetime | None = None,
) -> MatchObservation:
    played = played_at or datetime(2026, 8, 1, 12, tzinfo=UTC)
    return MatchObservation(
        source_id="research:ittf",
        source_record_id=record_id,
        source_revision=revision,
        source_locator=f"https://example.invalid/matches/{record_id}",
        source_evidence_sha256="a" * 64,
        observed_at=played + timedelta(hours=1),
        played_at=played,
        event_name="Open 2026",
        round_name="Quarterfinal",
        player_a=PlayerRef(a_id, a_name),
        player_b=PlayerRef(b_id, b_name),
        sets_a=sets_a,
        sets_b=sets_b,
    )


def _service(tmp_path) -> tuple[TableTennisRepository, TableTennisStatsService]:
    repository = TableTennisRepository(SQLiteStore(tmp_path / "дані з пробілом" / "stats.sqlite3"))
    return repository, TableTennisStatsService(repository)


def test_contract_is_strict_and_normalizes_utc() -> None:
    local = datetime(2026, 8, 1, 15, tzinfo=UTC)
    match = _match(played_at=local)
    assert match.played_at.tzinfo is UTC
    assert match.payload_sha256() == match.payload_sha256()

    with pytest.raises(TableTennisValidationError, match="winner"):
        _match(sets_a=2, sets_b=2)
    with pytest.raises(TableTennisValidationError, match="integer"):
        _match(sets_a=True)  # type: ignore[arg-type]
    with pytest.raises(TableTennisValidationError, match="distinct"):
        _match(a_id="same", b_id="same")
    with pytest.raises(TableTennisValidationError, match="control"):
        _match(a_name="bad\nname")


def test_exact_replay_is_idempotent(tmp_path) -> None:
    repository, service = _service(tmp_path)
    first = service.ingest(_match())
    replay = service.ingest(_match())

    assert first.disposition is IngestDisposition.INSERTED
    assert replay.disposition is IngestDisposition.REPLAYED
    assert first.payload_sha256 == replay.payload_sha256
    assert repository.revision_count() == 1


def test_same_revision_cannot_mutate_and_gaps_are_rejected(tmp_path) -> None:
    _, service = _service(tmp_path)
    service.ingest(_match())

    with pytest.raises(TableTennisRevisionError, match="cannot be mutated"):
        service.ingest(_match(sets_a=1, sets_b=3))
    with pytest.raises(TableTennisRevisionError, match="contiguous"):
        service.ingest(_match(revision=3, sets_a=1, sets_b=3))


def test_revision_supersedes_previous_result_and_survives_restart(tmp_path) -> None:
    db_path = tmp_path / "дані з пробілом" / "stats.sqlite3"
    first_repository = TableTennisRepository(SQLiteStore(db_path))
    first_service = TableTennisStatsService(first_repository)
    first_service.ingest(_match())
    result = first_service.ingest(_match(revision=2, sets_a=1, sets_b=3))
    assert result.disposition is IngestDisposition.REVISED
    assert first_repository.revision_count() == 2

    restarted = TableTennisStatsService(TableTennisRepository(SQLiteStore(db_path)))
    snapshot = restarted.snapshot()
    by_id = {player.player_id: player for player in snapshot.players}

    assert snapshot.current_match_count == 1
    assert by_id["player-a"].wins == 0
    assert by_id["player-a"].losses == 1
    assert by_id["player-b"].wins == 1
    assert by_id["player-b"].sets_for == 3


def test_statistics_are_deterministic_across_ingest_order(tmp_path) -> None:
    matches = [
        _match(record_id="m-2", a_id="p2", a_name="Two", b_id="p3", b_name="Three"),
        _match(record_id="m-1", a_id="p1", a_name="One", b_id="p2", b_name="Two"),
    ]
    _, service_a = _service(tmp_path / "a")
    _, service_b = _service(tmp_path / "b")
    service_a.ingest_many(matches)
    service_b.ingest_many(list(reversed(matches)))

    assert service_a.snapshot() == service_b.snapshot()
    p2 = {p.player_id: p for p in service_a.snapshot().players}["p2"]
    assert p2.matches == 2
    assert p2.wins == 1
    assert p2.losses == 1
    assert p2.win_rate_millionths == 500_000


def test_latest_match_name_is_used_deterministically(tmp_path) -> None:
    _, service = _service(tmp_path)
    older = _match(record_id="old", a_id="p1", a_name="Old Name", b_id="p2", b_name="Two")
    newer = _match(
        record_id="new",
        a_id="p1",
        a_name="New Name",
        b_id="p3",
        b_name="Three",
        played_at=datetime(2026, 8, 2, 12, tzinfo=UTC),
    )
    service.ingest_many([newer, older])

    by_id = {player.player_id: player for player in service.snapshot().players}
    assert by_id["p1"].display_name == "New Name"


def test_reports_are_utf8_explicit_and_csv_formula_safe(tmp_path) -> None:
    _, service = _service(tmp_path)
    service.ingest(_match(a_id="=formula", a_name="+Injected"))
    snapshot = service.snapshot()

    text = render_text_report(snapshot)
    assert text.filename == "table-tennis-statistics.txt"
    decoded = text.content.decode("utf-8")
    assert "Current matches: 1" in decoded
    assert "display_name" in decoded

    artifact = render_csv_report(snapshot)
    rows = list(csv.reader(io.StringIO(artifact.content.decode("utf-8"))))
    assert rows[0][0:2] == ["player_id", "display_name"]
    assert rows[1][0] == "'=formula"
    assert rows[1][1] == "'+Injected"
