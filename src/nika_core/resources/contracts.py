from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    cpu_percent: float
    memory_percent: float
    available_memory_bytes: int
    logical_cpu_count: int | None = None
    total_memory_bytes: int | None = None
    process_rss_bytes: int | None = None
    battery_percent: float | None = None
    power_plugged: bool | None = None


@dataclass(frozen=True, slots=True)
class ResourceBudget:
    scope: str
    owner_id: str
    max_concurrent: int = 1
    max_cpu_percent: float | None = None
    max_memory_percent: float | None = None


@dataclass(frozen=True, slots=True)
class ResourceCapacityStatus:
    """Read-only capacity truth derived from the existing ResourceManager authority."""

    budget: ResourceBudget
    snapshot: ResourceSnapshot
    active_count: int
    queued_count: int
    concurrency_headroom: int
    cpu_headroom_percent: float | None
    memory_headroom_percent: float | None
    pressure_reasons: tuple[str, ...]

    @property
    def under_pressure(self) -> bool:
        return bool(self.pressure_reasons)


class ResourceObserverPort(Protocol):
    def snapshot(self) -> ResourceSnapshot: ...
