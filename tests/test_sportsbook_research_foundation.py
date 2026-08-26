from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from nika_core.sportsbook_research import (
    CausalTime,
    Competition,
    Event,
    EventStatus,
    Market,
    OddsSnapshot,
    Participant,
    PeriodState,
    ScoreState,
    ScoreValue,
    Selection,
    Settlement,
    SettlementOutcome,
    SourceBatch,
    SportsbookConflictError,
    SportsbookCursorConflictError,
    SportsbookRepository,
    SportsbookResearchService,
    SportsbookSource,
    SportsbookValidationError,
)


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def timeline(base: datetime, seconds: int = 0) -> CausalTime:
    return CausalTime(
        event_at=base + timedelta(seconds=seconds),
        source_at=base + timedelta(seconds=seconds + 1),
        available_at=base + timedelta(seconds=seconds + 2),
    )


def seeded_repository(tmp_path: Path) -> tuple[SportsbookRepository, datetime]:
    repo = SportsbookRepository(Store(tmp_path / "дані з пробілами" / "sportsbook.db"))
    repo.initialize()
    repo.register_source(SportsbookSource("provider:a", "Провайдер А"))
    repo.put_competition(Competition("comp:1", "football", "Прем'єр ліга"))
    repo.put_participant(Participant("team:home", "Динамо"))
    repo.put_participant(Participant("team:away", "Шахтар"))
    base = datetime(2026, 8, 26, 18, 0, tzinfo=UTC)
    repo.put_event(
        Event(
            "event:1",
            "comp:1",
            ("team:home", "team:away"),
            base + timedelta(hours=2),
        )
    )
    repo.put_market(Market("market:1", "event:1", "match-winner", "Переможець матчу"))
    repo.put_selection(Selection("selection:home", "market:1", "Динамо", "team:home"))
    return repo, base


def test_causal_time_rejects_naive_and_out_of_order_timestamps() -> None:
    aware = datetime(2026, 8, 26, 18, 0, tzinfo=UTC)
    with pytest.raises(SportsbookValidationError, match="timezone-aware"):
        CausalTime(aware.replace(tzinfo=None), aware, aware)
    with pytest.raises(SportsbookValidationError, match="event_at"):
        CausalTime(aware + timedelta(seconds=1), aware, aware + timedelta(seconds=2))
    with pytest.raises(SportsbookValidationError, match="source_at"):
        CausalTime(aware, aware + timedelta(seconds=2), aware + timedelta(seconds=1))


def test_decimal_odds_are_exact_and_must_be_greater_than_one() -> None:
    base = datetime(2026, 8, 26, 18, 0, tzinfo=UTC)
    snapshot = OddsSnapshot(
        "odds:1", "provider:a", "selection:home", Decimal("1.9500"), timeline(base)
    )
    assert snapshot.decimal_odds == Decimal("1.9500")
    with pytest.raises(SportsbookValidationError, match="greater than 1"):
        OddsSnapshot("odds:bad", "provider:a", "selection:home", Decimal("1"), timeline(base))


def test_restart_dedup_conflict_and_unicode_windows_style_path(tmp_path: Path) -> None:
    repo, base = seeded_repository(tmp_path)
    snapshot = OddsSnapshot(
        "odds:1", "provider:a", "selection:home", Decimal("1.91"), timeline(base)
    )
    assert repo.record_odds(snapshot) is True
    assert repo.record_odds(snapshot) is False

    restarted = SportsbookRepository(Store(tmp_path / "дані з пробілами" / "sportsbook.db"))
    restarted.initialize()
    assert restarted.record_odds(snapshot) is False
    assert restarted.odds_as_of("selection:home", base + timedelta(seconds=2)) == snapshot

    conflicting = OddsSnapshot(
        "odds:1", "provider:a", "selection:home", Decimal("2.01"), timeline(base)
    )
    with pytest.raises(SportsbookConflictError, match="different content"):
        restarted.record_odds(conflicting)


def test_as_of_queries_use_local_availability_and_do_not_leak_future_data(tmp_path: Path) -> None:
    repo, base = seeded_repository(tmp_path)
    early = OddsSnapshot(
        "odds:early", "provider:a", "selection:home", Decimal("1.80"), timeline(base, 0)
    )
    future = OddsSnapshot(
        "odds:future",
        "provider:a",
        "selection:home",
        Decimal("2.20"),
        CausalTime(
            event_at=base + timedelta(seconds=1),
            source_at=base + timedelta(seconds=2),
            available_at=base + timedelta(minutes=10),
        ),
    )
    repo.record_odds(early)
    repo.record_odds(future)

    decision_time = base + timedelta(minutes=1)
    assert repo.odds_as_of("selection:home", decision_time) == early
    assert repo.odds_as_of("selection:home", base + timedelta(minutes=11)) == future


def test_score_period_and_settlement_are_durable_research_facts(tmp_path: Path) -> None:
    repo, base = seeded_repository(tmp_path)
    score = ScoreState(
        "score:1",
        "provider:a",
        "event:1",
        (ScoreValue("team:home", Decimal("1")), ScoreValue("team:away", Decimal("0"))),
        timeline(base),
    )
    period = PeriodState(
        "period:1",
        "provider:a",
        "event:1",
        "first-half",
        "34:12",
        EventStatus.LIVE,
        timeline(base),
    )
    settlement = Settlement(
        "settlement:1",
        "provider:a",
        "selection:home",
        SettlementOutcome.WON,
        timeline(base),
    )
    assert repo.record_score(score) is True
    assert repo.record_period(period) is True
    assert repo.record_settlement(settlement) is True
    assert repo.score_as_of("event:1", base + timedelta(seconds=2)) == score

    restarted, _ = seeded_repository(tmp_path)
    assert restarted.record_score(score) is False
    assert restarted.record_period(period) is False
    assert restarted.record_settlement(settlement) is False


def test_atomic_cursor_batch_supports_resume_and_rejects_stale_writer(tmp_path: Path) -> None:
    repo, base = seeded_repository(tmp_path)
    first = OddsSnapshot(
        "odds:batch:1", "provider:a", "selection:home", Decimal("1.75"), timeline(base)
    )
    repo.apply_batch(
        source_id="provider:a",
        expected_cursor=None,
        next_cursor="cursor-1",
        odds=(first,),
    )
    assert repo.cursor("provider:a") == "cursor-1"

    with pytest.raises(SportsbookCursorConflictError, match="cursor changed"):
        repo.apply_batch(
            source_id="provider:a",
            expected_cursor=None,
            next_cursor="cursor-stale",
            odds=(),
        )
    assert repo.cursor("provider:a") == "cursor-1"

    restarted = SportsbookRepository(Store(tmp_path / "дані з пробілами" / "sportsbook.db"))
    restarted.initialize()
    assert restarted.cursor("provider:a") == "cursor-1"
    restarted.apply_batch(
        source_id="provider:a",
        expected_cursor="cursor-1",
        next_cursor="cursor-2",
        odds=(first,),
    )
    assert restarted.cursor("provider:a") == "cursor-2"


def test_service_is_provider_neutral_and_read_only(tmp_path: Path) -> None:
    repo, base = seeded_repository(tmp_path)

    class FakeProvider:
        seen_cursors: list[str | None]

        def __init__(self) -> None:
            self.seen_cursors = []

        def fetch_updates(self, *, cursor: str | None) -> SourceBatch:
            self.seen_cursors.append(cursor)
            return SourceBatch(
                next_cursor="provider-cursor-1",
                odds=(
                    OddsSnapshot(
                        "odds:service",
                        "provider:a",
                        "selection:home",
                        Decimal("1.88"),
                        timeline(base),
                    ),
                ),
            )

    provider = FakeProvider()
    service = SportsbookResearchService(repo)
    batch = service.sync(SportsbookSource("provider:a", "Провайдер А"), provider)
    assert batch.next_cursor == "provider-cursor-1"
    assert provider.seen_cursors == [None]
    assert repo.cursor("provider:a") == "provider-cursor-1"
    assert not hasattr(provider, "place_bet")
