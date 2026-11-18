from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


class TriggerKind(StrEnum):
    DATE = "date"
    INTERVAL = "interval"
    CRON = "cron"


@dataclass(frozen=True, slots=True)
class ScheduledJob:
    job_id: str
    action_id: str
    trigger_kind: TriggerKind
    trigger: dict[str, Any]
    payload: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    coalesce: bool = True
    max_instances: int = 1
    misfire_grace_seconds: int | None = 60


class SchedulerPort(Protocol):
    def start(self) -> None: ...

    def shutdown(self, *, wait: bool = True) -> None: ...

    def upsert(self, job: ScheduledJob) -> None: ...

    def remove(self, job_id: str) -> bool: ...

    def pause(self, job_id: str) -> None: ...

    def resume(self, job_id: str) -> None: ...
