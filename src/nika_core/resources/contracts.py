from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    cpu_percent: float
    memory_percent: float
    available_memory_bytes: int
    disk_percent: float | None = None
    available_disk_bytes: int | None = None
    process_rss_bytes: int | None = None
    gpu_percent: float | None = None


@dataclass(frozen=True, slots=True)
class ResourceBudget:
    scope: str
    owner_id: str
    max_concurrent: int = 1
    max_cpu_percent: float | None = None
    max_memory_percent: float | None = None
    max_disk_percent: float | None = None
    max_gpu_percent: float | None = None
    max_process_memory_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class ResourceRequestIdentity:
    scope: str
    owner_id: str
    request_id: str
    product_project_id: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("scope", self.scope),
            ("owner_id", self.owner_id),
            ("request_id", self.request_id),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be empty")
        if self.product_project_id is not None and not self.product_project_id.strip():
            raise ValueError("product_project_id must not be empty when provided")


@dataclass(frozen=True, slots=True)
class ResourceProcessIdentity:
    process_id: int
    started_at: float

    def __post_init__(self) -> None:
        if isinstance(self.process_id, bool) or not isinstance(self.process_id, int):
            raise TypeError("process_id must be an integer")
        if self.process_id <= 0:
            raise ValueError("process_id must be positive")
        if isinstance(self.started_at, bool) or not isinstance(self.started_at, (int, float)):
            raise TypeError("started_at must be a number")
        normalized_started_at = float(self.started_at)
        if not math.isfinite(normalized_started_at) or normalized_started_at <= 0:
            raise ValueError("started_at must be a positive finite timestamp")
        object.__setattr__(self, "started_at", normalized_started_at)


class ResourceObserverPort(Protocol):
    def snapshot(self) -> ResourceSnapshot: ...


@runtime_checkable
class ResourceOwnerProbePort(Protocol):
    def current_process_identity(self) -> ResourceProcessIdentity: ...

    def is_process_alive(self, identity: ResourceProcessIdentity) -> bool: ...
