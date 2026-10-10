from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from typing import ContextManager, Protocol, cast

from nika_core.trading_research.contracts import EventTime, require_aware_utc

from .domain import (
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
    SportsbookObservation,
    SportsbookResearchError,
    SportsbookSource,
)

SPORTSBOOK_SCHEMA_VERSION = 1


class SQLiteConnectionHost(Protocol):
    """Structural subset implemented by the canonical nika_core.data.sqlite.SQLiteStore."""

    def connection(self) -> ContextManager[sqlite3.Connection]: ...


_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        "CREATE TABLE IF NOT EXISTS sportsbook_entities ("
        "entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, payload_json TEXT NOT NULL, "
        "payload_sha256 TEXT NOT NULL, PRIMARY KEY(entity_type, entity_id))",
        "CREATE TABLE IF NOT EXISTS sportsbook_observations ("
        "observation_key TEXT PRIMARY KEY, observation_type TEXT NOT NULL, "
        "source_id TEXT NOT NULL, event_id TEXT NOT NULL, market_id TEXT, "
        "event_at TEXT NOT NULL, source_at TEXT, available_at TEXT NOT NULL, "
        "source_sequence INTEGER NOT NULL, payload_json TEXT NOT NULL, "
        "payload_sha256 TEXT NOT NULL, recorded_at TEXT NOT NULL)",
        "CREATE INDEX IF NOT EXISTS idx_sportsbook_available "
        "ON sportsbook_observations(available_at, event_at, observation_key)",
        "CREATE INDEX IF NOT EXISTS idx_sportsbook_event "
        "ON sportsbook_observations(event_id, available_at, observation_key)",
    ),
}

_REQUIRED_ENTITY_COLUMNS = {
    "entity_type",
    "entity_id",
    "payload_json",
    "payload_sha256",
}
_REQUIRED_OBSERVATION_COLUMNS = {
    "observation_key",
    "observation_type",
    "source_id",
    "event_id",
    "market_id",
    "event_at",
    "source_at",
    "available_at",
    "source_sequence",
    "payload_json",
    "payload_sha256",
    "recorded_at",
}


def _json_text(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def _time_payload(time: EventTime) -> dict[str, str | None]:
    return {
        "event_at": time.event_at.isoformat(),
        "source_at": time.source_at.isoformat() if time.source_at is not None else None,
        "available_at": time.available_at.isoformat(),
    }


def _decode_time(payload: Mapping[str, object]) -> EventTime:
    source_at = payload.get("source_at")
    return EventTime(
        event_at=datetime.fromisoformat(str(payload["event_at"])),
        source_at=datetime.fromisoformat(str(source_at)) if source_at is not None else None,
        available_at=datetime.fromisoformat(str(payload["available_at"])),
    )


def _decimal_mapping(values: Mapping[str, object]) -> dict[str, Decimal]:
    return {str(key): Decimal(str(value)) for key, value in values.items()}


def _entity_record(entity: object) -> tuple[str, str, dict[str, object]]:
    if isinstance(entity, Sport):
        return "sport", entity.sport_id, {"sport_id": entity.sport_id, "name": entity.name}
    if isinstance(entity, Competition):
        return (
            "competition",
            entity.competition_id,
            {
                "competition_id": entity.competition_id,
                "sport_id": entity.sport_id,
                "name": entity.name,
            },
        )
    if isinstance(entity, Participant):
        return (
            "participant",
            entity.participant_id,
            {"participant_id": entity.participant_id, "name": entity.name},
        )
    if isinstance(entity, Event):
        return (
            "event",
            entity.event_id,
            {
                "event_id": entity.event_id,
                "competition_id": entity.competition_id,
                "participant_ids": list(entity.participant_ids),
                "scheduled_at": entity.scheduled_at.isoformat(),
            },
        )
    if isinstance(entity, Market):
        return (
            "market",
            entity.market_id,
            {
                "market_id": entity.market_id,
                "event_id": entity.event_id,
                "name": entity.name,
                "period_key": entity.period_key,
            },
        )
    if isinstance(entity, Selection):
        return (
            "selection",
            entity.selection_id,
            {
                "selection_id": entity.selection_id,
                "market_id": entity.market_id,
                "name": entity.name,
            },
        )
    if isinstance(entity, SportsbookSource):
        return (
            "source",
            entity.source_id,
            {
                "source_id": entity.source_id,
                "name": entity.name,
                "source_uri": entity.source_uri,
                "license_id": entity.license_id,
            },
        )
    raise TypeError(f"unsupported sportsbook entity: {type(entity).__name__}")


def _observation_payload(observation: SportsbookObservation) -> dict[str, object]:
    common: dict[str, object] = {
        "source_id": observation.source_id,
        "time": _time_payload(observation.time),
        "source_sequence": observation.source_sequence,
    }
    if isinstance(observation, OddsSnapshot):
        common.update(
            market_id=observation.market_id,
            prices={key: str(value) for key, value in sorted(observation.prices.items())},
        )
    elif isinstance(observation, ScoreState):
        common.update(
            event_id=observation.event_id,
            scores={key: value for key, value in sorted(observation.scores.items())},
        )
    elif isinstance(observation, PeriodState):
        common.update(
            event_id=observation.event_id,
            period_key=observation.period_key,
            ordinal=observation.ordinal,
            clock_seconds_remaining=(
                str(observation.clock_seconds_remaining)
                if observation.clock_seconds_remaining is not None
                else None
            ),
        )
    elif isinstance(observation, EventStatus):
        common.update(event_id=observation.event_id, status=observation.status.value)
    elif isinstance(observation, Settlement):
        common.update(
            market_id=observation.market_id,
            outcomes={key: str(value) for key, value in sorted(observation.outcomes.items())},
        )
    else:
        raise TypeError(f"unsupported sportsbook observation: {type(observation).__name__}")
    return common


def _observation_identity(observation: SportsbookObservation) -> str:
    subject_id = (
        observation.market_id
        if isinstance(observation, (OddsSnapshot, Settlement))
        else observation.event_id
    )
    identity = {
        "type": type(observation).__name__,
        "source_id": observation.source_id,
        "subject_id": subject_id,
        "event_at": observation.time.event_at.isoformat(),
        "source_at": (
            observation.time.source_at.isoformat()
            if observation.time.source_at is not None
            else None
        ),
        "source_sequence": observation.source_sequence,
    }
    return _digest(_json_text(identity))


def _decode_entity(entity_type: str, payload: Mapping[str, object]) -> object:
    if entity_type == "sport":
        return Sport(str(payload["sport_id"]), str(payload["name"]))
    if entity_type == "competition":
        return Competition(
            str(payload["competition_id"]),
            str(payload["sport_id"]),
            str(payload["name"]),
        )
    if entity_type == "participant":
        return Participant(str(payload["participant_id"]), str(payload["name"]))
    if entity_type == "event":
        participant_ids = cast(list[object], payload["participant_ids"])
        return Event(
            str(payload["event_id"]),
            str(payload["competition_id"]),
            tuple(str(item) for item in participant_ids),
            datetime.fromisoformat(str(payload["scheduled_at"])),
        )
    if entity_type == "market":
        return Market(
            str(payload["market_id"]),
            str(payload["event_id"]),
            str(payload["name"]),
            str(payload["period_key"]),
        )
    if entity_type == "selection":
        return Selection(
            str(payload["selection_id"]),
            str(payload["market_id"]),
            str(payload["name"]),
        )
    if entity_type == "source":
        source_uri = payload.get("source_uri")
        license_id = payload.get("license_id")
        return SportsbookSource(
            str(payload["source_id"]),
            str(payload["name"]),
            str(source_uri) if source_uri is not None else None,
            str(license_id) if license_id is not None else None,
        )
    raise SportsbookResearchError(f"unknown persisted entity type: {entity_type}")


def _decode_observation(
    observation_type: str,
    payload: Mapping[str, object],
) -> SportsbookObservation:
    time = _decode_time(cast(Mapping[str, object], payload["time"]))
    source_id = str(payload["source_id"])
    sequence = int(payload["source_sequence"])
    if observation_type == "OddsSnapshot":
        return OddsSnapshot(
            source_id,
            str(payload["market_id"]),
            time,
            _decimal_mapping(cast(Mapping[str, object], payload["prices"])),
            sequence,
        )
    if observation_type == "ScoreState":
        scores = cast(Mapping[str, object], payload["scores"])
        return ScoreState(
            source_id,
            str(payload["event_id"]),
            time,
            {str(key): int(value) for key, value in scores.items()},
            sequence,
        )
    if observation_type == "PeriodState":
        remaining = payload.get("clock_seconds_remaining")
        return PeriodState(
            source_id,
            str(payload["event_id"]),
            time,
            str(payload["period_key"]),
            int(payload["ordinal"]),
            Decimal(str(remaining)) if remaining is not None else None,
            sequence,
        )
    if observation_type == "EventStatus":
        return EventStatus(
            source_id,
            str(payload["event_id"]),
            time,
            EventStatusCode(str(payload["status"])),
            sequence,
        )
    if observation_type == "Settlement":
        return Settlement(
            source_id,
            str(payload["market_id"]),
            time,
            _decimal_mapping(cast(Mapping[str, object], payload["outcomes"])),
            sequence,
        )
    raise SportsbookResearchError(
        f"unknown persisted observation type: {observation_type}"
    )


class SQLiteSportsbookRepository:
    """Read-only-source research repository hosted by Nika's canonical SQLite store."""

    def __init__(self, store: SQLiteConnectionHost) -> None:
        self._store = store

    def initialize(self) -> None:
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS sportsbook_schema_migrations ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            rows = conn.execute(
                "SELECT version FROM sportsbook_schema_migrations ORDER BY version"
            ).fetchall()
            versions = [int(row["version"]) for row in rows]
            if versions and versions != list(range(1, max(versions) + 1)):
                raise SportsbookResearchError(
                    "sportsbook schema migration history is not contiguous"
                )
            current = versions[-1] if versions else 0
            if current > SPORTSBOOK_SCHEMA_VERSION:
                raise SportsbookResearchError(
                    "sportsbook schema is newer than this Nika version"
                )
            for version in range(current + 1, SPORTSBOOK_SCHEMA_VERSION + 1):
                statements = _MIGRATIONS.get(version)
                if statements is None:
                    raise SportsbookResearchError(
                        f"missing sportsbook schema migration {version}"
                    )
                for statement in statements:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO sportsbook_schema_migrations(version, applied_at) "
                    "VALUES (?, ?)",
                    (version, datetime.now(UTC).isoformat()),
                )
            self._validate_shape(conn)

    @staticmethod
    def _validate_shape(conn: sqlite3.Connection) -> None:
        entity_columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(sportsbook_entities)").fetchall()
        }
        observation_columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(sportsbook_observations)").fetchall()
        }
        if not _REQUIRED_ENTITY_COLUMNS.issubset(entity_columns):
            raise SportsbookResearchError("sportsbook entity schema is incomplete")
        if not _REQUIRED_OBSERVATION_COLUMNS.issubset(observation_columns):
            raise SportsbookResearchError("sportsbook observation schema is incomplete")

    def register_catalog(self, catalog: SportsbookCatalog) -> int:
        entities = (
            *catalog.sports,
            *catalog.competitions,
            *catalog.participants,
            *catalog.events,
            *catalog.markets,
            *catalog.selections,
            *catalog.sources,
        )
        inserted = 0
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._validate_shape(conn)
            for entity in entities:
                entity_type, entity_id, payload = _entity_record(entity)
                payload_json = _json_text(payload)
                payload_sha256 = _digest(payload_json)
                row = conn.execute(
                    "SELECT payload_sha256 FROM sportsbook_entities "
                    "WHERE entity_type = ? AND entity_id = ?",
                    (entity_type, entity_id),
                ).fetchone()
                if row is not None:
                    if str(row["payload_sha256"]) != payload_sha256:
                        raise SportsbookConflictError(
                            f"conflicting {entity_type} identity: {entity_id}"
                        )
                    continue
                conn.execute(
                    "INSERT INTO sportsbook_entities("
                    "entity_type, entity_id, payload_json, payload_sha256) "
                    "VALUES (?, ?, ?, ?)",
                    (entity_type, entity_id, payload_json, payload_sha256),
                )
                inserted += 1
            self._catalog_from_conn(conn)
        return inserted

    def load_catalog(self) -> SportsbookCatalog:
        with self._store.connection() as conn:
            self._validate_shape(conn)
            return self._catalog_from_conn(conn)

    @staticmethod
    def _catalog_from_conn(conn: sqlite3.Connection) -> SportsbookCatalog:
        decoded: dict[str, list[object]] = {
            "sport": [],
            "competition": [],
            "participant": [],
            "event": [],
            "market": [],
            "selection": [],
            "source": [],
        }
        rows = conn.execute(
            "SELECT entity_type, payload_json, payload_sha256 FROM sportsbook_entities "
            "ORDER BY entity_type, entity_id"
        ).fetchall()
        for row in rows:
            text = str(row["payload_json"])
            if _digest(text) != str(row["payload_sha256"]):
                raise SportsbookResearchError("sportsbook catalog payload integrity failure")
            entity_type = str(row["entity_type"])
            if entity_type not in decoded:
                raise SportsbookResearchError(
                    f"unknown persisted entity type: {entity_type}"
                )
            payload = json.loads(text)
            if not isinstance(payload, dict):
                raise SportsbookResearchError("sportsbook catalog payload must be an object")
            decoded[entity_type].append(_decode_entity(entity_type, payload))
        return SportsbookCatalog(
            sports=tuple(cast(list[Sport], decoded["sport"])),
            competitions=tuple(cast(list[Competition], decoded["competition"])),
            participants=tuple(cast(list[Participant], decoded["participant"])),
            events=tuple(cast(list[Event], decoded["event"])),
            markets=tuple(cast(list[Market], decoded["market"])),
            selections=tuple(cast(list[Selection], decoded["selection"])),
            sources=tuple(cast(list[SportsbookSource], decoded["source"])),
        )

    def ingest(self, observation: SportsbookObservation) -> bool:
        return self.ingest_many((observation,)) == 1

    def ingest_many(self, observations: Iterable[SportsbookObservation]) -> int:
        batch = tuple(observations)
        inserted = 0
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._validate_shape(conn)
            for observation in batch:
                event_id, market_id = self._validate_observation_refs(conn, observation)
                payload = _observation_payload(observation)
                payload_json = _json_text(payload)
                payload_sha256 = _digest(payload_json)
                observation_key = _observation_identity(observation)
                row = conn.execute(
                    "SELECT payload_sha256 FROM sportsbook_observations "
                    "WHERE observation_key = ?",
                    (observation_key,),
                ).fetchone()
                if row is not None:
                    if str(row["payload_sha256"]) != payload_sha256:
                        raise SportsbookConflictError(
                            "same source observation identity has conflicting payload"
                        )
                    continue
                conn.execute(
                    "INSERT INTO sportsbook_observations("
                    "observation_key, observation_type, source_id, event_id, market_id, "
                    "event_at, source_at, available_at, source_sequence, payload_json, "
                    "payload_sha256, recorded_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        observation_key,
                        type(observation).__name__,
                        observation.source_id,
                        event_id,
                        market_id,
                        observation.time.event_at.isoformat(),
                        (
                            observation.time.source_at.isoformat()
                            if observation.time.source_at is not None
                            else None
                        ),
                        observation.time.available_at.isoformat(),
                        observation.source_sequence,
                        payload_json,
                        payload_sha256,
                        datetime.now(UTC).isoformat(),
                    ),
                )
                inserted += 1
        return inserted

    @staticmethod
    def _entity_payload(
        conn: sqlite3.Connection,
        entity_type: str,
        entity_id: str,
    ) -> Mapping[str, object]:
        row = conn.execute(
            "SELECT payload_json, payload_sha256 FROM sportsbook_entities "
            "WHERE entity_type = ? AND entity_id = ?",
            (entity_type, entity_id),
        ).fetchone()
        if row is None:
            raise SportsbookResearchError(
                f"observation references unknown {entity_type}: {entity_id}"
            )
        text = str(row["payload_json"])
        if _digest(text) != str(row["payload_sha256"]):
            raise SportsbookResearchError("sportsbook entity payload integrity failure")
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise SportsbookResearchError("sportsbook entity payload must be an object")
        return payload

    @classmethod
    def _validate_observation_refs(
        cls,
        conn: sqlite3.Connection,
        observation: SportsbookObservation,
    ) -> tuple[str, str | None]:
        cls._entity_payload(conn, "source", observation.source_id)
        if isinstance(observation, (OddsSnapshot, Settlement)):
            market = cls._entity_payload(conn, "market", observation.market_id)
            event_id = str(market["event_id"])
            selections = (
                observation.prices
                if isinstance(observation, OddsSnapshot)
                else observation.outcomes
            )
            for selection_id in selections:
                selection = cls._entity_payload(conn, "selection", selection_id)
                if str(selection["market_id"]) != observation.market_id:
                    raise SportsbookResearchError(
                        f"selection {selection_id} does not belong to market "
                        f"{observation.market_id}"
                    )
            return event_id, observation.market_id
        event = cls._entity_payload(conn, "event", observation.event_id)
        if isinstance(observation, ScoreState):
            participant_ids = {
                str(item)
                for item in cast(list[object], event["participant_ids"])
            }
            unknown = [
                participant_id
                for participant_id in observation.scores
                if participant_id not in participant_ids
            ]
            if unknown:
                raise SportsbookResearchError(
                    f"score references participant outside event: {unknown[0]}"
                )
        return observation.event_id, None

    def observations_at(
        self,
        at: datetime,
        *,
        source_id: str | None = None,
        event_id: str | None = None,
    ) -> tuple[SportsbookObservation, ...]:
        at = require_aware_utc(at, "at")
        clauses = ["available_at <= ?"]
        params: list[object] = [at.isoformat()]
        if source_id is not None:
            source_id = source_id.strip()
            if not source_id:
                raise SportsbookResearchError("source_id must not be empty")
            clauses.append("source_id = ?")
            params.append(source_id)
        if event_id is not None:
            event_id = event_id.strip()
            if not event_id:
                raise SportsbookResearchError("event_id must not be empty")
            clauses.append("event_id = ?")
            params.append(event_id)
        sql = (
            "SELECT observation_type, payload_json, payload_sha256 "
            "FROM sportsbook_observations WHERE "
            + " AND ".join(clauses)
            + " ORDER BY available_at, event_at, observation_type, source_id, source_sequence, "
            "observation_key"
        )
        with self._store.connection() as conn:
            self._validate_shape(conn)
            rows = conn.execute(sql, tuple(params)).fetchall()
        result: list[SportsbookObservation] = []
        for row in rows:
            text = str(row["payload_json"])
            if _digest(text) != str(row["payload_sha256"]):
                raise SportsbookResearchError("sportsbook observation payload integrity failure")
            payload = json.loads(text)
            if not isinstance(payload, dict):
                raise SportsbookResearchError("sportsbook observation payload must be an object")
            result.append(_decode_observation(str(row["observation_type"]), payload))
        return tuple(result)
