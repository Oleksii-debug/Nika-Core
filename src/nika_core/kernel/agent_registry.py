from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from nika_core.data.sqlite import SQLiteStore


@dataclass(frozen=True, slots=True)
class AgentDefinition:
    agent_id: str
    name: str
    version: int
    goal: str

    def __post_init__(self) -> None:
        if not self.agent_id.strip():
            raise ValueError("agent_id must not be empty")
        if self.version < 1:
            raise ValueError("version must be >= 1")
        if not self.name.strip():
            raise ValueError("name must not be empty")


class AgentRegistry:
    def __init__(self, store: SQLiteStore | None = None) -> None:
        self._store = store
        self._agents: dict[str, AgentDefinition] = {}

    @property
    def count(self) -> int:
        if self._store is None:
            return len(self._agents)
        with self._store.connection() as conn:
            row = conn.execute("SELECT COUNT(DISTINCT agent_id) AS count FROM agents").fetchone()
        return int(row["count"])

    def register(self, definition: AgentDefinition) -> None:
        current = self._latest(definition.agent_id)
        if current is not None and definition.version <= current.version:
            raise ValueError("agent version must increase")
        if self._store is None:
            self._agents[definition.agent_id] = definition
            return
        with self._store.connection() as conn:
            conn.execute(
                "INSERT INTO agents(agent_id, version, name, goal, created_at) VALUES (?, ?, ?, ?, ?)",
                (
                    definition.agent_id,
                    definition.version,
                    definition.name,
                    definition.goal,
                    datetime.now(UTC).isoformat(),
                ),
            )

    def get(self, agent_id: str) -> AgentDefinition:
        current = self._latest(agent_id)
        if current is None:
            raise KeyError(f"Unknown agent: {agent_id}")
        return current

    def _latest(self, agent_id: str) -> AgentDefinition | None:
        if self._store is None:
            return self._agents.get(agent_id)
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT agent_id, name, version, goal FROM agents "
                "WHERE agent_id = ? ORDER BY version DESC LIMIT 1",
                (agent_id,),
            ).fetchone()
        if row is None:
            return None
        return AgentDefinition(row["agent_id"], row["name"], int(row["version"]), row["goal"])
