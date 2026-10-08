from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from nika_core.data.sqlite import SQLiteStore

_SQLITE_MAX_INT64 = (1 << 63) - 1


def _text_value(value: object, field_name: str, *, allow_blank: bool) -> str:
    if type(value) is not str:
        raise ValueError(f"{field_name} must be text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field_name} must be valid UTF-8 text") from exc
    if not allow_blank and not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    return value


def _stored_text(value: object, field_name: str, *, allow_blank: bool) -> str:
    try:
        return _text_value(value, field_name, allow_blank=allow_blank)
    except ValueError as exc:
        raise ValueError(f"invalid persisted agent {field_name}") from exc


def _reject_corrupt_identity_alias(conn: object, agent_id: str) -> None:
    row = conn.execute(
        "SELECT 1 FROM agents WHERE typeof(agent_id) != 'text' "
        "AND CAST(agent_id AS TEXT) = ? LIMIT 1",
        (agent_id,),
    ).fetchone()
    if row is not None:
        raise ValueError("invalid persisted agent agent_id")


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    agent_id: str
    name: str
    version: int
    goal: str

    def __post_init__(self) -> None:
        _text_value(self.agent_id, "agent_id", allow_blank=False)
        if type(self.version) is not int or not 1 <= self.version <= _SQLITE_MAX_INT64:
            raise ValueError("version must be a positive SQLite-sized integer")
        _text_value(self.name, "name", allow_blank=False)
        _text_value(self.goal, "goal", allow_blank=True)


def _snapshot_definition(value: object) -> AgentDefinition:
    if type(value) is not AgentDefinition:
        raise TypeError("definition must be an exact AgentDefinition")
    return AgentDefinition(value.agent_id, value.name, value.version, value.goal)


class AgentRegistry:
    def __init__(self, store: SQLiteStore | None = None) -> None:
        self._store = store
        self._agents: dict[str, AgentDefinition] = {}

    @property
    def count(self) -> int:
        if self._store is None:
            return len(self._agents)
        with self._store.connection() as conn:
            corrupt = conn.execute(
                "SELECT 1 FROM agents WHERE typeof(agent_id) != 'text' LIMIT 1"
            ).fetchone()
            if corrupt is not None:
                raise ValueError("invalid persisted agent agent_id")
            row = conn.execute(
                "SELECT COUNT(DISTINCT agent_id) AS count FROM agents"
            ).fetchone()
        return int(row["count"])

    def register(self, definition: AgentDefinition) -> None:
        canonical = _snapshot_definition(definition)
        if self._store is None:
            current = self._latest(canonical.agent_id)
            if current is not None and canonical.version <= current.version:
                raise ValueError("agent version must increase")
            self._agents[canonical.agent_id] = canonical
            return

        with self._store.connection() as conn:
            # Serialize the version check and insert across independent registry instances.
            # A deferred transaction would still allow multiple writers to observe the same
            # previous version before one of them commits.
            conn.execute("BEGIN IMMEDIATE")
            _reject_corrupt_identity_alias(conn, canonical.agent_id)
            row = conn.execute(
                "SELECT agent_id, name, version, goal FROM agents WHERE agent_id = ? "
                "ORDER BY version DESC LIMIT 1",
                (canonical.agent_id,),
            ).fetchone()
            if row is not None:
                _stored_text(row["agent_id"], "agent_id", allow_blank=False)
                _stored_text(row["name"], "name", allow_blank=False)
                current_version = _stored_version(row["version"])
                _stored_text(row["goal"], "goal", allow_blank=True)
                if canonical.version <= current_version:
                    raise ValueError("agent version must increase")
            conn.execute(
                "INSERT INTO agents(agent_id, version, name, goal, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    canonical.agent_id,
                    canonical.version,
                    canonical.name,
                    canonical.goal,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def get(self, agent_id: str) -> AgentDefinition:
        normalized_id = _text_value(agent_id, "agent_id", allow_blank=False)
        current = self._latest(normalized_id)
        if current is None:
            raise KeyError(f"Unknown agent: {normalized_id}")
        return _snapshot_definition(current)

    def list_latest(self) -> tuple[AgentDefinition, ...]:
        if self._store is None:
            return tuple(
                _snapshot_definition(self._agents[key]) for key in sorted(self._agents)
            )
        with self._store.connection() as conn:
            rows = conn.execute(
                "SELECT a.agent_id, a.name, a.version, a.goal FROM agents AS a "
                "JOIN (SELECT agent_id, MAX(version) AS version FROM agents GROUP BY agent_id) AS latest "
                "ON latest.agent_id = a.agent_id AND latest.version = a.version "
                "ORDER BY a.agent_id"
            ).fetchall()
        return tuple(
            AgentDefinition(
                _stored_text(row["agent_id"], "agent_id", allow_blank=False),
                _stored_text(row["name"], "name", allow_blank=False),
                _stored_version(row["version"]),
                _stored_text(row["goal"], "goal", allow_blank=True),
            )
            for row in rows
        )

    def _latest(self, agent_id: str) -> AgentDefinition | None:
        if self._store is None:
            return self._agents.get(agent_id)
        with self._store.connection() as conn:
            _reject_corrupt_identity_alias(conn, agent_id)
            row = conn.execute(
                "SELECT agent_id, name, version, goal FROM agents "
                "WHERE agent_id = ? ORDER BY version DESC LIMIT 1",
                (agent_id,),
            ).fetchone()
        if row is None:
            return None
        return AgentDefinition(
            _stored_text(row["agent_id"], "agent_id", allow_blank=False),
            _stored_text(row["name"], "name", allow_blank=False),
            _stored_version(row["version"]),
            _stored_text(row["goal"], "goal", allow_blank=True),
        )


def _stored_version(value: object) -> int:
    if type(value) is not int or not 1 <= value <= _SQLITE_MAX_INT64:
        raise ValueError("invalid persisted agent version")
    return value
