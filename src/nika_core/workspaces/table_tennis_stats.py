from __future__ import annotations

import csv
import hashlib
import io
import json
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from nika_core.data.sqlite import SQLiteStore
from nika_core.research.models import ResearchEvidence

_SCHEMA_VERSION = 1


class TableTennisStatsError(ValueError):
    pass


class StaleMatchObservationError(TableTennisStatsError):
    pass


class TableTennisDataIntegrityError(RuntimeError):
    pass


class MatchIngestDisposition(StrEnum):
    CREATED = "created"
    UNCHANGED = "unchanged"
    UPDATED = "updated"


def _text(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{label} must be text")
    value = unicodedata.normalize("NFC", value).strip()
    if not value:
        raise TableTennisStatsError(f"{label} must not be empty")
    return value


def _timestamp(value: datetime | str, label: str) -> str:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise TableTennisStatsError(f"{label} must be an ISO timestamp") from exc
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise TypeError(f"{label} must be datetime or ISO text")
    if parsed.tzinfo is None:
        raise TableTennisStatsError(f"{label} must be timezone-aware")
    return parsed.astimezone(UTC).isoformat()


def _plain_int(value: object, label: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{label} must be an integer")
    if value < 0:
        raise TableTennisStatsError(f"{label} must be non-negative")
    return value


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def _match_id(source_id: str, source_match_id: str) -> str:
    raw = f"{source_id}\0{source_match_id}".encode()
    return "tt-" + hashlib.sha256(raw).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class GameScore:
    player_a: int
    player_b: int

    def __post_init__(self) -> None:
        _plain_int(self.player_a, "player_a score")
        _plain_int(self.player_b, "player_b score")
        if self.player_a == self.player_b:
            raise TableTennisStatsError("a completed game cannot be tied")


@dataclass(frozen=True, slots=True)
class MatchObservation:
    source_match_id: str
    document_id: str
    competition: str
    played_at: datetime | str
    player_a: str
    player_b: str
    games: tuple[GameScore, ...]
    evidence: ResearchEvidence

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_match_id", _text(self.source_match_id, "source_match_id"))
        object.__setattr__(self, "document_id", _text(self.document_id, "document_id"))
        object.__setattr__(self, "competition", _text(self.competition, "competition"))
        object.__setattr__(self, "player_a", _text(self.player_a, "player_a"))
        object.__setattr__(self, "player_b", _text(self.player_b, "player_b"))
        object.__setattr__(self, "played_at", _timestamp(self.played_at, "played_at"))
        if self.player_a.casefold() == self.player_b.casefold():
            raise TableTennisStatsError("players must be distinct")
        if not isinstance(self.games, tuple) or not self.games:
            raise TableTennisStatsError("games must be a non-empty tuple")
        if any(not isinstance(game, GameScore) for game in self.games):
            raise TypeError("games must contain GameScore values")
        if not isinstance(self.evidence, ResearchEvidence):
            raise TypeError("evidence must be ResearchEvidence")
        _text(self.evidence.source_id, "evidence source_id")
        _timestamp(self.evidence.observed_at, "evidence observed_at")
        if self.game_wins[0] == self.game_wins[1]:
            raise TableTennisStatsError("a completed match must have a winner")

    @property
    def source_id(self) -> str:
        return _text(self.evidence.source_id, "evidence source_id")

    @property
    def observed_at(self) -> str:
        return _timestamp(self.evidence.observed_at, "evidence observed_at")

    @property
    def game_wins(self) -> tuple[int, int]:
        a = sum(game.player_a > game.player_b for game in self.games)
        return a, len(self.games) - a

    def payload(self) -> dict[str, Any]:
        return {
            "competition": self.competition,
            "games": [[game.player_a, game.player_b] for game in self.games],
            "played_at": self.played_at,
            "player_a": self.player_a,
            "player_b": self.player_b,
        }


@dataclass(frozen=True, slots=True)
class MatchSnapshot:
    match_id: str
    source_id: str
    source_match_id: str
    version: int
    document_id: str
    observed_at: str
    competition: str
    played_at: str
    player_a: str
    player_b: str
    games: tuple[GameScore, ...]

    @property
    def game_wins(self) -> tuple[int, int]:
        a = sum(game.player_a > game.player_b for game in self.games)
        return a, len(self.games) - a

    @property
    def winner(self) -> str:
        a, b = self.game_wins
        return self.player_a if a > b else self.player_b


@dataclass(frozen=True, slots=True)
class MatchIngestResult:
    match_id: str
    version: int
    disposition: MatchIngestDisposition


@dataclass(frozen=True, slots=True)
class PlayerStats:
    player: str
    matches: int
    wins: int
    losses: int
    games_won: int
    games_lost: int

    @property
    def win_rate(self) -> float:
        return self.wins / self.matches if self.matches else 0.0


def initialize_table_tennis_schema(store: SQLiteStore) -> None:
    with store.connection() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS table_tennis_schema_migrations "
            "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        row = conn.execute(
            "SELECT MAX(version) AS version FROM table_tennis_schema_migrations"
        ).fetchone()
        current = int(row["version"] or 0)
        if current > _SCHEMA_VERSION:
            raise RuntimeError(
                f"table tennis schema {current} is newer than supported {_SCHEMA_VERSION}"
            )
        if current == 0:
            conn.execute(
                "CREATE TABLE table_tennis_matches ("
                "match_id TEXT PRIMARY KEY, source_id TEXT NOT NULL, source_match_id TEXT NOT NULL,"
                "current_version INTEGER NOT NULL, current_fingerprint TEXT NOT NULL,"
                "last_seen_at TEXT NOT NULL, latest_document_id TEXT NOT NULL,"
                "UNIQUE(source_id, source_match_id))"
            )
            conn.execute(
                "CREATE TABLE table_tennis_match_revisions ("
                "match_id TEXT NOT NULL, version INTEGER NOT NULL, fingerprint TEXT NOT NULL,"
                "payload_json TEXT NOT NULL, observed_at TEXT NOT NULL, document_id TEXT NOT NULL,"
                "PRIMARY KEY(match_id, version),"
                "FOREIGN KEY(match_id) REFERENCES table_tennis_matches(match_id))"
            )
            conn.execute(
                "INSERT INTO table_tennis_schema_migrations(version, applied_at) VALUES (?, ?)",
                (_SCHEMA_VERSION, datetime.now(UTC).isoformat()),
            )


class TableTennisStatsRepository:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    def ingest(self, observation: MatchObservation) -> MatchIngestResult:
        payload = observation.payload()
        fingerprint = _fingerprint(payload)
        match_id = _match_id(observation.source_id, observation.source_match_id)
        with self.store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM table_tennis_matches WHERE source_id=? AND source_match_id=?",
                (observation.source_id, observation.source_match_id),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO table_tennis_matches VALUES (?,?,?,?,?,?,?)",
                    (
                        match_id,
                        observation.source_id,
                        observation.source_match_id,
                        1,
                        fingerprint,
                        observation.observed_at,
                        observation.document_id,
                    ),
                )
                self._revision(conn, match_id, 1, fingerprint, payload, observation)
                return MatchIngestResult(match_id, 1, MatchIngestDisposition.CREATED)
            if row["match_id"] != match_id:
                raise TableTennisDataIntegrityError("durable match identity mismatch")
            version = int(row["current_version"])
            if row["current_fingerprint"] == fingerprint:
                if observation.observed_at >= _timestamp(row["last_seen_at"], "last_seen_at"):
                    conn.execute(
                        "UPDATE table_tennis_matches SET last_seen_at=?,latest_document_id=? "
                        "WHERE match_id=?",
                        (observation.observed_at, observation.document_id, match_id),
                    )
                return MatchIngestResult(match_id, version, MatchIngestDisposition.UNCHANGED)
            if observation.observed_at < _timestamp(row["last_seen_at"], "last_seen_at"):
                raise StaleMatchObservationError(
                    "older evidence cannot replace newer durable match facts"
                )
            next_version = version + 1
            self._revision(conn, match_id, next_version, fingerprint, payload, observation)
            cursor = conn.execute(
                "UPDATE table_tennis_matches SET current_version=?,current_fingerprint=?,"
                "last_seen_at=?,latest_document_id=? "
                "WHERE match_id=? AND current_version=? AND current_fingerprint=?",
                (
                    next_version,
                    fingerprint,
                    observation.observed_at,
                    observation.document_id,
                    match_id,
                    version,
                    row["current_fingerprint"],
                ),
            )
            if cursor.rowcount != 1:
                raise TableTennisDataIntegrityError("concurrent match update lost authority")
            return MatchIngestResult(match_id, next_version, MatchIngestDisposition.UPDATED)

    @staticmethod
    def _revision(
        conn: Any,
        match_id: str,
        version: int,
        fingerprint: str,
        payload: dict[str, Any],
        observation: MatchObservation,
    ) -> None:
        conn.execute(
            "INSERT INTO table_tennis_match_revisions VALUES (?,?,?,?,?,?)",
            (
                match_id,
                version,
                fingerprint,
                _json(payload),
                observation.observed_at,
                observation.document_id,
            ),
        )

    def list_current(self) -> tuple[MatchSnapshot, ...]:
        with self.store.connection() as conn:
            rows = conn.execute(
                "SELECT m.*,r.fingerprint,r.payload_json,r.observed_at,r.document_id "
                "FROM table_tennis_matches m JOIN table_tennis_match_revisions r "
                "ON r.match_id=m.match_id AND r.version=m.current_version ORDER BY m.match_id"
            ).fetchall()
            return tuple(self._snapshot(dict(row), current=True) for row in rows)

    def history(self, match_id: str) -> tuple[MatchSnapshot, ...]:
        with self.store.connection() as conn:
            parent = conn.execute(
                "SELECT * FROM table_tennis_matches WHERE match_id=?", (match_id,)
            ).fetchone()
            if parent is None:
                raise KeyError(match_id)
            rows = conn.execute(
                "SELECT * FROM table_tennis_match_revisions WHERE match_id=? ORDER BY version",
                (match_id,),
            ).fetchall()
            if len(rows) != int(parent["current_version"]):
                raise TableTennisDataIntegrityError("match revision history is not contiguous")
            result: list[MatchSnapshot] = []
            for expected, row in enumerate(rows, 1):
                if int(row["version"]) != expected:
                    raise TableTennisDataIntegrityError("match revision versions are not contiguous")
                data = dict(parent)
                data.update(dict(row))
                data["current_version"] = row["version"]
                data["current_fingerprint"] = row["fingerprint"]
                result.append(self._snapshot(data, current=False))
            if rows[-1]["fingerprint"] != parent["current_fingerprint"]:
                raise TableTennisDataIntegrityError(
                    "current fingerprint does not bind latest revision"
                )
            return tuple(result)

    @staticmethod
    def _snapshot(data: dict[str, Any], *, current: bool) -> MatchSnapshot:
        try:
            source_id = _text(data["source_id"], "source_id")
            source_match_id = _text(data["source_match_id"], "source_match_id")
            match_id = _text(data["match_id"], "match_id")
            if match_id != _match_id(source_id, source_match_id):
                raise TableTennisDataIntegrityError("durable match id failed source binding")
            version = data["current_version"]
            if type(version) is not int or version < 1:
                raise TableTennisDataIntegrityError("durable match version is invalid")
            payload = json.loads(data["payload_json"])
            expected = {"competition", "games", "played_at", "player_a", "player_b"}
            if not isinstance(payload, dict) or set(payload) != expected:
                raise TableTennisDataIntegrityError("durable match payload shape is invalid")
            fingerprint = _text(data["current_fingerprint"], "fingerprint")
            if data["fingerprint"] != fingerprint or _fingerprint(payload) != fingerprint:
                raise TableTennisDataIntegrityError("durable match fingerprint mismatch")
            games_raw = payload["games"]
            if not isinstance(games_raw, list) or not games_raw:
                raise TableTennisDataIntegrityError("durable games are invalid")
            games: list[GameScore] = []
            for item in games_raw:
                if not isinstance(item, list) or len(item) != 2:
                    raise TableTennisDataIntegrityError("durable game shape is invalid")
                try:
                    games.append(
                        GameScore(
                            _plain_int(item[0], "durable player_a score"),
                            _plain_int(item[1], "durable player_b score"),
                        )
                    )
                except (TypeError, TableTennisStatsError) as exc:
                    raise TableTennisDataIntegrityError(
                        "durable match game score is invalid"
                    ) from exc
            observed = data["last_seen_at"] if current else data["observed_at"]
            document = data["latest_document_id"] if current else data["document_id"]
            snapshot = MatchSnapshot(
                match_id,
                source_id,
                source_match_id,
                version,
                _text(document, "document_id"),
                _timestamp(observed, "observed_at"),
                _text(payload["competition"], "competition"),
                _timestamp(payload["played_at"], "played_at"),
                _text(payload["player_a"], "player_a"),
                _text(payload["player_b"], "player_b"),
                tuple(games),
            )
            if snapshot.player_a.casefold() == snapshot.player_b.casefold():
                raise TableTennisDataIntegrityError("durable players are not distinct")
            if snapshot.game_wins[0] == snapshot.game_wins[1]:
                raise TableTennisDataIntegrityError("durable completed match has no winner")
            return snapshot
        except TableTennisDataIntegrityError:
            raise
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TableTennisDataIntegrityError(
                "durable match state violates workspace contract"
            ) from exc


class TableTennisStatsWorkspace:
    def __init__(self, store: SQLiteStore) -> None:
        initialize_table_tennis_schema(store)
        self.repository = TableTennisStatsRepository(store)

    def ingest(self, observation: MatchObservation) -> MatchIngestResult:
        return self.repository.ingest(observation)

    def player_statistics(self) -> tuple[PlayerStats, ...]:
        totals: dict[str, list[int]] = {}
        for match in self.repository.list_current():
            a, b = match.game_wins
            for player, won, lost in (
                (match.player_a, a, b),
                (match.player_b, b, a),
            ):
                row = totals.setdefault(player, [0, 0, 0, 0, 0])
                row[0] += 1
                row[3] += won
                row[4] += lost
                if player == match.winner:
                    row[1] += 1
                else:
                    row[2] += 1
        return tuple(
            PlayerStats(player, *values)
            for player, values in sorted(totals.items(), key=lambda item: item[0].casefold())
        )

    def render_text_report(self) -> str:
        stats = self.player_statistics()
        lines = ["Table Tennis Stats Collector", f"Players: {len(stats)}"]
        for item in stats:
            lines += [
                "",
                f"Player: {item.player}",
                f"Matches: {item.matches}",
                f"Wins: {item.wins}",
                f"Losses: {item.losses}",
                f"Games won: {item.games_won}",
                f"Games lost: {item.games_lost}",
                f"Win rate: {item.win_rate:.3f}",
            ]
        return "\n".join(lines) + "\n"

    def render_csv_report(self) -> str:
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\r\n")
        writer.writerow(
            ("player", "matches", "wins", "losses", "games_won", "games_lost", "win_rate")
        )
        for item in self.player_statistics():
            writer.writerow(
                (
                    _safe_cell(item.player),
                    item.matches,
                    item.wins,
                    item.losses,
                    item.games_won,
                    item.games_lost,
                    f"{item.win_rate:.6f}",
                )
            )
        return output.getvalue()


def _safe_cell(value: str) -> str:
    value = unicodedata.normalize("NFC", value).strip()
    return "'" + value if value and value[0] in ("=", "+", "-", "@") else value
