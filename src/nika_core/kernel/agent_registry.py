from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class AgentDefinition:
    agent_id: str
    name: str
    version: int
    goal: str
    def __post_init__(self) -> None:
        if not self.agent_id.strip(): raise ValueError("agent_id must not be empty")
        if self.version < 1: raise ValueError("version must be >= 1")
        if not self.name.strip(): raise ValueError("name must not be empty")

class AgentRegistry:
    def __init__(self) -> None: self._agents: dict[str, AgentDefinition] = {}
    @property
    def count(self) -> int: return len(self._agents)
    def register(self, definition: AgentDefinition) -> None:
        current=self._agents.get(definition.agent_id)
        if current is not None and definition.version <= current.version:
            raise ValueError("agent version must increase")
        self._agents[definition.agent_id]=definition
    def get(self, agent_id: str) -> AgentDefinition:
        try: return self._agents[agent_id]
        except KeyError as exc: raise KeyError(f"Unknown agent: {agent_id}") from exc
