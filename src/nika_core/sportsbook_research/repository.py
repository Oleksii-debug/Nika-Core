from __future__ import annotations

import json
import sqlite3
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from .models import (
    CausalTime,
    Competition,
    Event,
    Market,
    OddsSnapshot,
    Participant,
    PeriodState,
    ScoreState,
    ScoreValue,
    Selection,
    Settlement,
    SportsbookSource,
    SportsbookValidationError,
)


SPORTSBOOK_SCHEMA_VERSION = 1


class SportsbookConflictError(RuntimeError):
    """A durable identity was replayed with different immutable content."""


class SportsbookCursorConflictError(RuntimeError):
    """A provider sync attempted to advance from a stale cursor."""


class ConnectionStore(Protocol):
    def connection(self) -> AbstractContextManager[sqlite3.Connection]: ...


_SCHEMA_V1 = (
    "CREATE TABLE IF NOT EXISTS sportsbook_sources ("
    "source_id TEXT PRIMARY KEY, name TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS sportsbook_competitions ("
    "competition_id TEXT PRIMARY KEY, sport TEXT NOT NULL, name TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS sportsbook_participants ("
    "participant_id TEXT PRIMARY KEY, name TEXT NOT NULL)",
    "CREATE TABLE IF NOT EXISTS sportsbook_events ("
    "event_id TEXT PRIMARY KEY, competition_id TEXT NOT NULL, starts_at TEXT NOT NULL, "
    "status TEXT NOT NULL, FOREIGN KEY(competition_id) "
    "REFERENCES sportsbook_competitions(competition_id))",
    "CREATE TABLE IF NOT EXISTS sportsbook_event_participants ("
    "event_id TEXT NOT NULL, position INTEGER NOT NULL, participant_id TEXT NOT NULL, "
    "PRIMARY KEY(event_id, position), UNIQUE(event_id, participant_id), "
    "FOREIGN KEY(event_id) REFERENCES sportsbook_events(event_id), "
    "FOREIGN KEY(participant_id) REFERENCES sportsbook_participants(participant_id))",
    "CREATE TABLE IF NOT EXISTS sportsbook_markets ("
    "market_id TEXT PRIMARY KEY, event_id TEXT NOT NULL, market_key TEXT NOT NULL, "
    "name TEXT NOT NULL, status TEXT NOT NULL, "
    "FOREIGN KEY(event_id) REFERENCES sportsbook_events(event_id))",
    "CREATE TABLE IF NOT EXISTS sportsbook_selections ("
    "selection_id TEXT PRIMARY KEY, market_id TEXT NOT NULL, label TEXT NOT NULL, "
    "participant_id TEXT, FOREIGN KEY(market_id) REFERENCES sportsbook_markets(market_id), "
    "FOREIGN KEY(participant_id) REFERENCES sportsbook_participants(participant_id))",
    "CREATE TABLE IF NOT EXISTS sportsbook_odds ("
    "snapshot_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, selection_id TEXT NOT NULL, "
    "decimal_odds TEXT NOT NULL, event_at TEXT NOT NULL, source_at TEXT NOT NULL, "
    "available_at TEXT NOT NULL, FOREIGN KEY(source_id) REFERENCES sportsbook_sources(source_id), "
    "FOREIGN KEY(selection_id) REFERENCES sportsbook_selections(selection_id))",
    "CREATE INDEX IF NOT EXISTS sportsbook_odds_asof_idx ON sportsbook_odds "
    "(selection_id, available_at, source_at, event_at, snapshot_id)",
    "CREATE TABLE IF NOT EXISTS sportsbook_scores ("
    "score_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, event_id TEXT NOT NULL, "
    "values_json TEXT NOT NULL, event_at TEXT NOT NULL, source_at TEXT NOT NULL, "
    "available_at TEXT NOT NULL, FOREIGN KEY(source_id) REFERENCES sportsbook_sources(source_id), "
    "FOREIGN KEY(event_id) REFERENCES sportsbook_events(event_id))",
    "CREATE INDEX IF NOT EXISTS sportsbook_scores_asof_idx ON sportsbook_scores "
    "(event_id, available_at, source_at, event_at, score_id)",
    "CREATE TABLE IF NOT EXISTS sportsbook_period_states ("
    "period_state_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, event_id TEXT NOT NULL, "
    "period TEXT NOT NULL, clock TEXT, status TEXT NOT NULL, event_at TEXT NOT NULL, "
    "source_at TEXT NOT NULL, available_at TEXT NOT NULL, "
    "FOREIGN KEY(source_id) REFERENCES sportsbook_sources(source_id), "
    "FOREIGN KEY(event_id) REFERENCES sportsbook_events(event_id))",
    "CREATE TABLE IF NOT EXISTS sportsbook_settlements ("
    "settlement_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, selection_id TEXT NOT NULL, "
    "outcome TEXT NOT NULL, event_at TEXT NOT NULL, source_at TEXT NOT NULL, "
    "available_at TEXT NOT NULL, FOREIGN KEY(source_id) REFERENCES sportsbook_sources(source_id), "
    "FOREIGN KEY(selection_id) REFERENCES sportsbook_selections(selection_id))",
    "CREATE TABLE IF NOT EXISTS sportsbook_source_cursors ("
    "source_id TEXT PRIMARY KEY, cursor TEXT NOT NULL, advanced_at TEXT NOT NULL, "
    "FOREIGN KEY(source_id) REFERENCES sportsbook_sources(source_id))",
)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _timeline(row: sqlite3.Row) -> CausalTime:
    return CausalTime(
        event_at=datetime.fromisoformat(row["event_at"]),
        source_at=datetime.fromisoformat(row["source_at"]),
        available_at=datetime.fromisoformat(row["available_at"]),
    )


class SportsbookRepository:
    """Durable, provider-neutral, read-only sportsbook research store."""

    def __init__(self, store: ConnectionStore) -> None:
        self._store = store

    def initialize(self) -> None:
        with self._store.connection() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS sportsbook_schema_migrations ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            row = conn.execute(
                "SELECT MAX(version) AS version FROM sportsbook_schema_migrations"
            ).fetchone()
            current = int(row["version"] or 0)
            if current > SPORTSBOOK_SCHEMA_VERSION:
                raise RuntimeError(
                    "sportsbook schema "
                    f"{current} is newer than supported schema {SPORTSBOOK_SCHEMA_VERSION}"
                )
            if current < 1:
                for statement in _SCHEMA_V1:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO sportsbook_schema_migrations(version, applied_at) VALUES (?, ?)",
                    (1, datetime.now(UTC).isoformat()),
                )

    @staticmethod
    def _insert_exact(
        conn: sqlite3.Connection,
        *,
        table: str,
        identity_column: str,
        identity: str,
        values: dict[str, str | int | None],
    ) -> bool:
        columns = tuple(values)
        row = conn.execute(
            f"SELECT {', '.join(columns)} FROM {table} WHERE {identity_column} = ?",
            (identity,),
        ).fetchone()
        expected = tuple(values[column] for column in columns)
        if row is not None:
            actual = tuple(row[column] for column in columns)
            if actual != expected:
                raise SportsbookConflictError(
                    f"{table} identity {identity!r} already exists with different content"
                )
            return False
        all_columns = (identity_column, *columns)
        placeholders = ", ".join("?" for _ in all_columns)
        conn.execute(
            f"INSERT INTO {table} ({', '.join(all_columns)}) VALUES ({placeholders})",
            (identity, *expected),
        )
        return True

    def register_source(self, source: SportsbookSource) -> bool:
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self._insert_exact(
                conn,
                table="sportsbook_sources",
                identity_column="source_id",
                identity=source.source_id,
                values={"name": source.name},
            )

    def put_competition(self, competition: Competition) -> bool:
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self._insert_exact(
                conn,
                table="sportsbook_competitions",
                identity_column="competition_id",
                identity=competition.competition_id,
                values={"sport": competition.sport, "name": competition.name},
            )

    def put_participant(self, participant: Participant) -> bool:
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self._insert_exact(
                conn,
                table="sportsbook_participants",
                identity_column="participant_id",
                identity=participant.participant_id,
                values={"name": participant.name},
            )

    def put_event(self, event: Event) -> bool:
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            inserted = self._insert_exact(
                conn,
                table="sportsbook_events",
                identity_column="event_id",
                identity=event.event_id,
                values={
                    "competition_id": event.competition_id,
                    "starts_at": _iso(event.starts_at),
                    "status": event.status.value,
                },
            )
            rows = conn.execute(
                "SELECT position, participant_id FROM sportsbook_event_participants "
                "WHERE event_id = ? ORDER BY position",
                (event.event_id,),
            ).fetchall()
            expected = tuple(enumerate(event.participant_ids))
            if rows:
                actual = tuple((int(row["position"]), row["participant_id"]) for row in rows)
                if actual != expected:
                    raise SportsbookConflictError(
                        "event "
                        f"{event.event_id!r} participant identity differs from durable content"
                    )
            else:
                conn.executemany(
                    "INSERT INTO sportsbook_event_participants(event_id, position, participant_id) "
                    "VALUES (?, ?, ?)",
                    (
                        (event.event_id, position, participant_id)
                        for position, participant_id in expected
                    ),
                )
            return inserted

    def put_market(self, market: Market) -> bool:
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self._insert_exact(
                conn,
                table="sportsbook_markets",
                identity_column="market_id",
                identity=market.market_id,
                values={
                    "event_id": market.event_id,
                    "market_key": market.key,
                    "name": market.name,
                    "status": market.status.value,
                },
            )

    def put_selection(self, selection: Selection) -> bool:
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self._insert_exact(
                conn,
                table="sportsbook_selections",
                identity_column="selection_id",
                identity=selection.selection_id,
                values={
                    "market_id": selection.market_id,
                    "label": selection.label,
                    "participant_id": selection.participant_id,
                },
            )

    def record_odds(self, snapshot: OddsSnapshot) -> bool:
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self._record_odds(conn, snapshot)

    def _record_odds(self, conn: sqlite3.Connection, snapshot: OddsSnapshot) -> bool:
        return self._insert_exact(
            conn,
            table="sportsbook_odds",
            identity_column="snapshot_id",
            identity=snapshot.snapshot_id,
            values={
                "source_id": snapshot.source_id,
                "selection_id": snapshot.selection_id,
                "decimal_odds": str(snapshot.decimal_odds),
                "event_at": _iso(snapshot.timeline.event_at),
                "source_at": _iso(snapshot.timeline.source_at),
                "available_at": _iso(snapshot.timeline.available_at),
            },
        )

    def record_score(self, state: ScoreState) -> bool:
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self._record_score(conn, state)

    def _record_score(self, conn: sqlite3.Connection, state: ScoreState) -> bool:
        values_json = json.dumps(
            [(value.participant_id, str(value.value)) for value in state.values],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return self._insert_exact(
            conn,
            table="sportsbook_scores",
            identity_column="score_id",
            identity=state.score_id,
            values={
                "source_id": state.source_id,
                "event_id": state.event_id,
                "values_json": values_json,
                "event_at": _iso(state.timeline.event_at),
                "source_at": _iso(state.timeline.source_at),
                "available_at": _iso(state.timeline.available_at),
            },
        )

    def record_period(self, state: PeriodState) -> bool:
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self._record_period(conn, state)

    def _record_period(self, conn: sqlite3.Connection, state: PeriodState) -> bool:
        return self._insert_exact(
            conn,
            table="sportsbook_period_states",
            identity_column="period_state_id",
            identity=state.period_state_id,
            values={
                "source_id": state.source_id,
                "event_id": state.event_id,
                "period": state.period,
                "clock": state.clock,
                "status": state.status.value,
                "event_at": _iso(state.timeline.event_at),
                "source_at": _iso(state.timeline.source_at),
                "available_at": _iso(state.timeline.available_at),
            },
        )

    def record_settlement(self, settlement: Settlement) -> bool:
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self._record_settlement(conn, settlement)

    def _record_settlement(self, conn: sqlite3.Connection, settlement: Settlement) -> bool:
        return self._insert_exact(
            conn,
            table="sportsbook_settlements",
            identity_column="settlement_id",
            identity=settlement.settlement_id,
            values={
                "source_id": settlement.source_id,
                "selection_id": settlement.selection_id,
                "outcome": settlement.outcome.value,
                "event_at": _iso(settlement.timeline.event_at),
                "source_at": _iso(settlement.timeline.source_at),
                "available_at": _iso(settlement.timeline.available_at),
            },
        )

    def odds_as_of(self, selection_id: str, available_at: datetime) -> OddsSnapshot | None:
        available = _iso(available_at)
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT * FROM sportsbook_odds WHERE selection_id = ? AND available_at <= ? "
                "ORDER BY available_at DESC, source_at DESC, event_at DESC, snapshot_id DESC "
                "LIMIT 1",
                (selection_id, available),
            ).fetchone()
        if row is None:
            return None
        return OddsSnapshot(
            snapshot_id=row["snapshot_id"],
            source_id=row["source_id"],
            selection_id=row["selection_id"],
            decimal_odds=Decimal(row["decimal_odds"]),
            timeline=_timeline(row),
        )

    def score_as_of(self, event_id: str, available_at: datetime) -> ScoreState | None:
        available = _iso(available_at)
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT * FROM sportsbook_scores WHERE event_id = ? AND available_at <= ? "
                "ORDER BY available_at DESC, source_at DESC, event_at DESC, score_id DESC LIMIT 1",
                (event_id, available),
            ).fetchone()
        if row is None:
            return None
        values = tuple(
            ScoreValue(participant_id=participant_id, value=Decimal(value))
            for participant_id, value in json.loads(row["values_json"])
        )
        return ScoreState(
            score_id=row["score_id"],
            source_id=row["source_id"],
            event_id=row["event_id"],
            values=values,
            timeline=_timeline(row),
        )

    def cursor(self, source_id: str) -> str | None:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT cursor FROM sportsbook_source_cursors WHERE source_id = ?", (source_id,)
            ).fetchone()
        return None if row is None else str(row["cursor"])

    def apply_batch(
        self,
        *,
        source_id: str,
        expected_cursor: str | None,
        next_cursor: str,
        odds: tuple[OddsSnapshot, ...] = (),
        scores: tuple[ScoreState, ...] = (),
        periods: tuple[PeriodState, ...] = (),
        settlements: tuple[Settlement, ...] = (),
    ) -> None:
        if not next_cursor.strip():
            raise SportsbookValidationError("next_cursor must not be empty")
        records = (*odds, *scores, *periods, *settlements)
        if any(record.source_id != source_id for record in records):
            raise SportsbookValidationError("every batch record must match source_id")
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT cursor FROM sportsbook_source_cursors WHERE source_id = ?", (source_id,)
            ).fetchone()
            actual_cursor = None if row is None else str(row["cursor"])
            if actual_cursor != expected_cursor:
                raise SportsbookCursorConflictError(
                    f"source {source_id!r} cursor changed from {expected_cursor!r} "
                    f"to {actual_cursor!r}"
                )
            for snapshot in odds:
                self._record_odds(conn, snapshot)
            for state in scores:
                self._record_score(conn, state)
            for state in periods:
                self._record_period(conn, state)
            for settlement in settlements:
                self._record_settlement(conn, settlement)
            advanced_at = datetime.now(UTC).isoformat()
            if row is None:
                conn.execute(
                    "INSERT INTO sportsbook_source_cursors(source_id, cursor, advanced_at) "
                    "VALUES (?, ?, ?)",
                    (source_id, next_cursor, advanced_at),
                )
            else:
                conn.execute(
                    "UPDATE sportsbook_source_cursors SET cursor = ?, advanced_at = ? "
                    "WHERE source_id = ?",
                    (next_cursor, advanced_at, source_id),
                )
