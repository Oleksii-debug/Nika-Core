from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.research.models import FreshnessState, ResearchEvidence, SourceKind
from nika_core.workspaces.table_tennis_stats import (
    GameScore,
    MatchIngestDisposition,
    MatchObservation,
    StaleMatchObservationError,
    TableTennisDataIntegrityError,
    TableTennisStatsError,
    TableTennisStatsWorkspace,
)


def _evidence(*, observed: datetime, locator: str = "https://source.test/match") -> ResearchEvidence:
    return ResearchEvidence(
        source_id="source-1",
        source_kind=SourceKind.HTTP,
        locator=locator,
        observed_at=observed.isoformat(),
        freshness=FreshnessState.CURRENT,
    )


def _match(
    *,
    observed: datetime,
    document_id: str = "doc-1",
    games: tuple[GameScore, ...] | None = None,
    player_a: str = "Олена",
    player_b: str = "Marta",
) -> MatchObservation:
    return MatchObservation(
        source_match_id="match-42",
        document_id=document_id,
        competition="Кубок Ужгорода",
        played_at=datetime(2026, 8, 26, 18, tzinfo=UTC),
        player_a=player_a,
        player_b=player_b,
        games=games or (GameScore(11, 7), GameScore(9, 11), GameScore(11, 8)),
        evidence=_evidence(observed=observed),
    )


def _workspace(path: Path) -> TableTennisStatsWorkspace:
    store = SQLiteStore(path)
    store.initialize()
    return TableTennisStatsWorkspace(store)


def test_ingest_deduplicates_then_versions_changed_facts_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "ніка статистика.db"
    t0 = datetime(2026, 8, 26, 19, tzinfo=UTC)
    workspace = _workspace(path)

    first = workspace.ingest(_match(observed=t0))
    same = workspace.ingest(_match(observed=t0 + timedelta(minutes=1), document_id="doc-2"))
    changed = workspace.ingest(
        _match(
            observed=t0 + timedelta(minutes=2),
            document_id="doc-3",
            games=(GameScore(11, 7), GameScore(9, 11), GameScore(11, 6)),
        )
    )

    assert first.disposition is MatchIngestDisposition.CREATED
    assert same == type(same)(first.match_id, 1, MatchIngestDisposition.UNCHANGED)
    assert changed == type(changed)(first.match_id, 2, MatchIngestDisposition.UPDATED)

    restarted = _workspace(path)
    history = restarted.repository.history(first.match_id)
    assert tuple(item.version for item in history) == (1, 2)
    assert history[0].document_id == "doc-1"
    assert history[1].document_id == "doc-3"
    assert restarted.repository.list_current()[0].document_id == "doc-3"


def test_older_changed_evidence_cannot_replace_newer_facts(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "nika.db")
    newer = datetime(2026, 8, 26, 20, tzinfo=UTC)
    workspace.ingest(_match(observed=newer))

    with pytest.raises(StaleMatchObservationError):
        workspace.ingest(
            _match(
                observed=newer - timedelta(minutes=5),
                games=(GameScore(11, 5), GameScore(11, 4)),
            )
        )


def test_older_unchanged_evidence_does_not_regress_latest_provenance(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "nika.db")
    newer = datetime(2026, 8, 26, 20, tzinfo=UTC)
    result = workspace.ingest(_match(observed=newer, document_id="new-doc"))
    workspace.ingest(
        _match(
            observed=newer - timedelta(minutes=5),
            document_id="old-doc",
        )
    )
    current = workspace.repository.list_current()[0]
    assert current.match_id == result.match_id
    assert current.document_id == "new-doc"
    assert current.observed_at == newer.isoformat()


def test_statistics_and_accessible_reports_are_deterministic(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "nika.db")
    t0 = datetime(2026, 8, 26, 20, tzinfo=UTC)
    workspace.ingest(_match(observed=t0))

    stats = workspace.player_statistics()
    assert [(item.player, item.matches, item.wins) for item in stats] == [
        ("Marta", 1, 0),
        ("Олена", 1, 1),
    ]
    text = workspace.render_text_report()
    assert "Player: Олена\nMatches: 1\nWins: 1" in text
    assert "Win rate: 1.000" in text

    csv_text = workspace.render_csv_report()
    assert csv_text.startswith("player,matches,wins,losses,games_won,games_lost,win_rate\r\n")
    assert "Олена,1,1,0,2,1,1.000000\r\n" in csv_text


def test_csv_neutralizes_spreadsheet_formula_names(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "nika.db")
    workspace.ingest(
        _match(
            observed=datetime(2026, 8, 26, 20, tzinfo=UTC),
            player_a="=HYPERLINK(\"https://evil.invalid\")",
        )
    )
    csv_text = workspace.render_csv_report()
    assert "'=HYPERLINK" in csv_text


def test_source_locator_and_credentials_are_not_persisted(tmp_path: Path) -> None:
    path = tmp_path / "nika.db"
    store = SQLiteStore(path)
    store.initialize()
    workspace = TableTennisStatsWorkspace(store)
    secret_locator = "https://user:synthetic-secret@example.invalid/match?token=canary"
    observation = _match(observed=datetime(2026, 8, 26, 20, tzinfo=UTC))
    observation = MatchObservation(
        source_match_id=observation.source_match_id,
        document_id=observation.document_id,
        competition=observation.competition,
        played_at=observation.played_at,
        player_a=observation.player_a,
        player_b=observation.player_b,
        games=observation.games,
        evidence=_evidence(
            observed=datetime(2026, 8, 26, 20, tzinfo=UTC),
            locator=secret_locator,
        ),
    )
    workspace.ingest(observation)

    raw = path.read_bytes()
    assert b"synthetic-secret" not in raw
    assert b"token=canary" not in raw


def test_corrupted_durable_game_type_fails_closed_after_restart(tmp_path: Path) -> None:
    path = tmp_path / "nika.db"
    workspace = _workspace(path)
    result = workspace.ingest(_match(observed=datetime(2026, 8, 26, 20, tzinfo=UTC)))

    with sqlite3.connect(path) as conn:
        payload = json.loads(
            conn.execute(
                "SELECT payload_json FROM table_tennis_match_revisions "
                "WHERE match_id=? AND version=1",
                (result.match_id,),
            ).fetchone()[0]
        )
        payload["games"][0][0] = "11"
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        conn.execute(
            "UPDATE table_tennis_match_revisions SET payload_json=?,fingerprint=? "
            "WHERE match_id=? AND version=1",
            (canonical, digest, result.match_id),
        )
        conn.execute(
            "UPDATE table_tennis_matches SET current_fingerprint=? WHERE match_id=?",
            (digest, result.match_id),
        )

    restarted = _workspace(path)
    with pytest.raises(TableTennisDataIntegrityError, match="game score is invalid"):
        restarted.repository.list_current()


@pytest.mark.parametrize(
    "games",
    [
        (GameScore(11, 9), GameScore(9, 11)),
        (GameScore(11, 9),),
    ],
)
def test_completed_match_requires_a_winner(
    games: tuple[GameScore, ...],
) -> None:
    kwargs = dict(
        source_match_id="match-x",
        document_id="doc-x",
        competition="Cup",
        played_at=datetime(2026, 8, 26, tzinfo=UTC),
        player_a="A",
        player_b="B",
        games=games,
        evidence=_evidence(observed=datetime(2026, 8, 26, tzinfo=UTC)),
    )
    if len(games) == 2:
        with pytest.raises(TableTennisStatsError, match="winner"):
            MatchObservation(**kwargs)
    else:
        assert MatchObservation(**kwargs).game_wins == (1, 0)


def test_bool_score_and_naive_timestamp_are_rejected() -> None:
    with pytest.raises(TypeError):
        GameScore(True, 7)
    with pytest.raises(TableTennisStatsError, match="timezone-aware"):
        _match(observed=datetime(2026, 8, 26, 20))
