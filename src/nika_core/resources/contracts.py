from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


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


class ResourceObserverPort(Protocol):
    def snapshot(self) -> ResourceSnapshot: ...
