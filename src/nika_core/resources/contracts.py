from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    cpu_percent: float
    memory_percent: float
    available_memory_bytes: int


@dataclass(frozen=True, slots=True)
class ResourceBudget:
    scope: str
    owner_id: str
    max_concurrent: int = 1
    max_cpu_percent: float | None = None
    max_memory_percent: float | None = None


class ResourceObserverPort(Protocol):
    def snapshot(self) -> ResourceSnapshot: ...
