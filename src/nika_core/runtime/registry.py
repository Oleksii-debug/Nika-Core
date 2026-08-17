from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from nika_core.runtime.contracts import AgentRuntimePort, RuntimeCapability


@dataclass(frozen=True, slots=True)
class RuntimeDescriptor:
    runtime_id: str
    capabilities: frozenset[RuntimeCapability]


class RuntimeRegistry:
    def __init__(self) -> None:
        self._runtimes: dict[str, AgentRuntimePort] = {}

    def register(self, runtime: AgentRuntimePort) -> None:
        runtime_id = runtime.runtime_id.strip()
        if not runtime_id:
            raise ValueError("runtime_id must not be empty")
        if runtime_id in self._runtimes:
            raise ValueError(f"Runtime already registered: {runtime_id}")
        self._runtimes[runtime_id] = runtime

    def get(self, runtime_id: str) -> AgentRuntimePort:
        try:
            return self._runtimes[runtime_id]
        except KeyError as exc:
            raise KeyError(f"Unknown runtime: {runtime_id}") from exc

    def select(self, required: Iterable[RuntimeCapability]) -> AgentRuntimePort:
        required_set = frozenset(required)
        candidates = [
            runtime
            for runtime in self._runtimes.values()
            if required_set <= runtime.capabilities
        ]
        if not candidates:
            names = ", ".join(sorted(item.value for item in required_set))
            raise LookupError(f"No runtime satisfies required capabilities: {names}")
        return sorted(candidates, key=lambda runtime: runtime.runtime_id)[0]

    def describe(self) -> tuple[RuntimeDescriptor, ...]:
        return tuple(
            RuntimeDescriptor(runtime.runtime_id, runtime.capabilities)
            for runtime in sorted(self._runtimes.values(), key=lambda item: item.runtime_id)
        )
