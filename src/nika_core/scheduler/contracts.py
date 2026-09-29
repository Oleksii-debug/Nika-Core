from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


class TriggerKind(StrEnum):
    DATE = "date"
    INTERVAL = "interval"
    CRON = "cron"


@dataclass(frozen=True, slots=True)
class ScheduleIdentity:
    scope: str
    owner_id: str
    dedup_key: str
    product_project_id: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("scope", self.scope),
            ("owner_id", self.owner_id),
            ("dedup_key", self.dedup_key),
        ):
            if not value.strip():
                raise ValueError(f"{name} must not be empty")
        if self.product_project_id is not None and not self.product_project_id.strip():
            raise ValueError("product_project_id must not be empty when provided")


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
    identity: ScheduleIdentity | None = None


class SchedulerPort(Protocol):
    def start(self) -> None: ...

    def shutdown(self, *, wait: bool = True) -> None: ...

    def upsert(self, job: ScheduledJob) -> None: ...

    def remove(self, job_id: str) -> bool: ...

    def pause(self, job_id: str) -> None: ...

    def resume(self, job_id: str) -> None: ...
