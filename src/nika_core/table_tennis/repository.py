from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from nika_core.data.sqlite import SQLiteStore

from .contracts import IngestDisposition, IngestResult, MatchObservation, PlayerRef


class TableTennisIntegrityError(RuntimeError):
    """Raised when durable table-tennis state is internally inconsistent."""


class TableTennisRevisionError(ValueError):
    """Raised when an upstream revision attempts rollback, gaps, or mutation."""


_SCHEMA_VERSION = 1
_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """
        CREATE TABLE IF NOT EXISTS table_tennis_matches (
            source_id TEXT NOT NULL,
            source_record_id TEXT NOT NULL,
            source_revision INTEGER NOT NULL CHECK (source_revision >= 1),
            payload_sha256 TEXT NOT NULL,
            source_locator TEXT NOT NULL,
            source_evidence_sha256 TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            played_at TEXT NOT NULL,
            event_name TEXT NOT NULL,
            round_name TEXT,
            player_a_id TEXT NOT NULL,
            player_a_name TEXT NOT NULL,
            player_b_id TEXT NOT NULL,
            player_b_name TEXT NOT NULL,
            sets_a INTEGER NOT NULL CHECK (sets_a >= 0),
            sets_b INTEGER NOT NULL CHECK (sets_b >= 0),
            ingested_at TEXT NOT NULL,
            PRIMARY KEY (source_id, source_record_id, source_revision)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS table_tennis_match_heads (
            source_id TEXT NOT NULL,
            source_record_id TEXT NOT NULL,
            latest_revision INTEGER NOT NULL CHECK (latest_revision >= 1),
            latest_payload_sha256 TEXT NOT NULL,
            PRIMARY KEY (source_id, source_record_id),
            FOREIGN KEY (source_id, source_record_id, latest_revision)
                REFERENCES table_tennis_matches(source_id, source_record_id, source_revision)
                ON UPDATE RESTRICT ON DELETE RESTRICT
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_table_tennis_matches_played_at
        ON table_tennis_matches(played_at, source_id, source_record_id, source_revision)
        """,
    )
}


class TableTennisRepository:
    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def initialize(self) -> None:
        with self._store.connection() as conn:
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS table_tennis_schema_migrations ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            row = conn.execute(
                "SELECT MAX(version) AS version FROM table_tennis_schema_migrations"
            ).fetchone()
            current = int(row["version"] or 0)
            if current > _SCHEMA_VERSION:
                raise RuntimeError(
                    "table-tennis schema "
                    f"{current} is newer than supported schema {_SCHEMA_VERSION}"
                )
            for version in range(current + 1, _SCHEMA_VERSION + 1):
                statements = _MIGRATIONS.get(version)
                if statements is None:
                    raise RuntimeError(f"missing table-tennis migration {version}")
                for statement in statements:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO table_tennis_schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, datetime.now(UTC).isoformat()),
                )

    def ingest(self, observation: MatchObservation) -> IngestResult:
        if not isinstance(observation, MatchObservation):
            raise TypeError("observation must be a MatchObservation")
        self.initialize()
        payload_sha256 = observation.payload_sha256()
        with self._store.connection() as conn:
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("BEGIN IMMEDIATE")
            head = conn.execute(
                """
                SELECT latest_revision, latest_payload_sha256
                FROM table_tennis_match_heads
                WHERE source_id = ? AND source_record_id = ?
                """,
                (observation.source_id, observation.source_record_id),
            ).fetchone()
            if head is None:
                return self._insert_initial(conn, observation, payload_sha256)
            return self._ingest_against_head(conn, head, observation, payload_sha256)

    def list_current_matches(self) -> tuple[MatchObservation, ...]:
        self.initialize()
        with self._store.connection() as conn:
            rows = conn.execute(
                """
                SELECT m.*, h.latest_payload_sha256 AS head_payload_sha256
                FROM table_tennis_match_heads AS h
                JOIN table_tennis_matches AS m
                  ON m.source_id = h.source_id
                 AND m.source_record_id = h.source_record_id
                 AND m.source_revision = h.latest_revision
                ORDER BY m.played_at, m.source_id, m.source_record_id, m.source_revision
                """
            ).fetchall()
            head_count = int(
                conn.execute("SELECT COUNT(*) AS count FROM table_tennis_match_heads").fetchone()[
                    "count"
                ]
            )
        if len(rows) != head_count:
            raise TableTennisIntegrityError(
                "one or more table-tennis heads have no matching record"
            )
        observations: list[MatchObservation] = []
        for row in rows:
            observation = self._observation_from_row(row)
            actual = observation.payload_sha256()
            if actual != row["payload_sha256"]:
                raise TableTennisIntegrityError("stored table-tennis match payload hash mismatch")
            if actual != row["head_payload_sha256"]:
                raise TableTennisIntegrityError("table-tennis head payload hash mismatch")
            observations.append(observation)
        return tuple(observations)

    def revision_count(self) -> int:
        self.initialize()
        with self._store.connection() as conn:
            row = conn.execute("SELECT COUNT(*) AS count FROM table_tennis_matches").fetchone()
        return int(row["count"])

    def _insert_initial(
        self,
        conn: sqlite3.Connection,
        observation: MatchObservation,
        payload_sha256: str,
    ) -> IngestResult:
        prior = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM table_tennis_matches
            WHERE source_id = ? AND source_record_id = ?
            """,
            (observation.source_id, observation.source_record_id),
        ).fetchone()
        if int(prior["count"]) != 0:
            raise TableTennisIntegrityError("match revisions exist without a durable head")
        if observation.source_revision != 1:
            raise TableTennisRevisionError("the first source revision must be 1")
        self._insert_record(conn, observation, payload_sha256)
        conn.execute(
            """
            INSERT INTO table_tennis_match_heads(
                source_id, source_record_id, latest_revision, latest_payload_sha256
            ) VALUES (?, ?, ?, ?)
            """,
            (
                observation.source_id,
                observation.source_record_id,
                observation.source_revision,
                payload_sha256,
            ),
        )
        return self._result(observation, payload_sha256, IngestDisposition.INSERTED)

    def _ingest_against_head(
        self,
        conn: sqlite3.Connection,
        head: sqlite3.Row,
        observation: MatchObservation,
        payload_sha256: str,
    ) -> IngestResult:
        latest_revision = int(head["latest_revision"])
        latest_payload_sha256 = str(head["latest_payload_sha256"])
        current_row = conn.execute(
            """
            SELECT * FROM table_tennis_matches
            WHERE source_id = ? AND source_record_id = ? AND source_revision = ?
            """,
            (observation.source_id, observation.source_record_id, latest_revision),
        ).fetchone()
        if current_row is None:
            raise TableTennisIntegrityError("table-tennis head references a missing record")
        current = self._observation_from_row(current_row)
        current_payload = current.payload_sha256()
        if (
            current_payload != current_row["payload_sha256"]
            or current_payload != latest_payload_sha256
        ):
            raise TableTennisIntegrityError(
                "current table-tennis record failed payload verification"
            )

        if observation.source_revision == latest_revision:
            if payload_sha256 != latest_payload_sha256:
                raise TableTennisRevisionError("an existing source revision cannot be mutated")
            return self._result(observation, payload_sha256, IngestDisposition.REPLAYED)
        if observation.source_revision < latest_revision:
            return self._replay_historical(conn, observation, payload_sha256)
        if observation.source_revision != latest_revision + 1:
            raise TableTennisRevisionError("source revisions must be contiguous")
        self._insert_record(conn, observation, payload_sha256)
        updated = conn.execute(
            """
            UPDATE table_tennis_match_heads
            SET latest_revision = ?, latest_payload_sha256 = ?
            WHERE source_id = ? AND source_record_id = ? AND latest_revision = ?
            """,
            (
                observation.source_revision,
                payload_sha256,
                observation.source_id,
                observation.source_record_id,
                latest_revision,
            ),
        )
        if updated.rowcount != 1:
            raise TableTennisIntegrityError("table-tennis head changed during revision update")
        return self._result(observation, payload_sha256, IngestDisposition.REVISED)

    def _replay_historical(
        self,
        conn: sqlite3.Connection,
        observation: MatchObservation,
        payload_sha256: str,
    ) -> IngestResult:
        historical_row = conn.execute(
            """
            SELECT * FROM table_tennis_matches
            WHERE source_id = ? AND source_record_id = ? AND source_revision = ?
            """,
            (
                observation.source_id,
                observation.source_record_id,
                observation.source_revision,
            ),
        ).fetchone()
        if historical_row is None:
            raise TableTennisIntegrityError(
                "table-tennis head has a missing historical revision"
            )
        historical = self._observation_from_row(historical_row)
        historical_payload = historical.payload_sha256()
        if historical_payload != historical_row["payload_sha256"]:
            raise TableTennisIntegrityError(
                "historical table-tennis record failed payload verification"
            )
        if payload_sha256 != historical_payload:
            raise TableTennisRevisionError(
                "an existing historical source revision cannot be mutated"
            )
        return self._result(observation, payload_sha256, IngestDisposition.REPLAYED)

    @staticmethod
    def _insert_record(
        conn: sqlite3.Connection,
        observation: MatchObservation,
        payload_sha256: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO table_tennis_matches(
                source_id, source_record_id, source_revision, payload_sha256,
                source_locator, source_evidence_sha256, observed_at, played_at,
                event_name, round_name,
                player_a_id, player_a_name, player_b_id, player_b_name,
                sets_a, sets_b, ingested_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                observation.source_id,
                observation.source_record_id,
                observation.source_revision,
                payload_sha256,
                observation.source_locator,
                observation.source_evidence_sha256,
                observation.observed_at.isoformat(),
                observation.played_at.isoformat(),
                observation.event_name,
                observation.round_name,
                observation.player_a.player_id,
                observation.player_a.display_name,
                observation.player_b.player_id,
                observation.player_b.display_name,
                observation.sets_a,
                observation.sets_b,
                datetime.now(UTC).isoformat(),
            ),
        )

    @staticmethod
    def _observation_from_row(row: sqlite3.Row) -> MatchObservation:
        try:
            observed_at = datetime.fromisoformat(str(row["observed_at"]))
            played_at = datetime.fromisoformat(str(row["played_at"]))
            return MatchObservation(
                source_id=str(row["source_id"]),
                source_record_id=str(row["source_record_id"]),
                source_revision=int(row["source_revision"]),
                source_locator=str(row["source_locator"]),
                source_evidence_sha256=str(row["source_evidence_sha256"]),
                observed_at=observed_at,
                played_at=played_at,
                event_name=str(row["event_name"]),
                round_name=None if row["round_name"] is None else str(row["round_name"]),
                player_a=PlayerRef(str(row["player_a_id"]), str(row["player_a_name"])),
                player_b=PlayerRef(str(row["player_b_id"]), str(row["player_b_name"])),
                sets_a=int(row["sets_a"]),
                sets_b=int(row["sets_b"]),
            )
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise TableTennisIntegrityError("invalid stored table-tennis match record") from exc

    @staticmethod
    def _result(
        observation: MatchObservation,
        payload_sha256: str,
        disposition: IngestDisposition,
    ) -> IngestResult:
        return IngestResult(
            source_id=observation.source_id,
            source_record_id=observation.source_record_id,
            source_revision=observation.source_revision,
            payload_sha256=payload_sha256,
            disposition=disposition,
        )
