from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum


class MemoryScope(StrEnum):
    TASK = "task"
    AGENT = "agent"
    WORKSPACE = "workspace"
    USER = "user"


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    scope: MemoryScope
    owner_id: str
    key: str
    value: str
    created_at: datetime
    expires_at: datetime | None = None

    @classmethod
    def create(
        cls,
        *,
        scope: MemoryScope,
        owner_id: str,
        key: str,
        value: str,
        ttl_seconds: int | None = None,
        now: datetime | None = None,
    ) -> MemoryRecord:
        if not owner_id.strip() or not key.strip():
            raise ValueError("memory owner_id and key must be non-empty")
        if ttl_seconds is not None and ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive when provided")
        created_at = now or datetime.now(timezone.utc)
        expires_at = (
            created_at + timedelta(seconds=ttl_seconds) if ttl_seconds is not None else None
        )
        return cls(scope, owner_id, key, value, created_at, expires_at)

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (now or datetime.now(timezone.utc)) >= self.expires_at


@dataclass(frozen=True, slots=True)
class ScheduledJob:
    job_id: str
    task_id: str
    next_run_at: datetime
    interval_seconds: int | None = None

    def __post_init__(self) -> None:
        if not self.job_id.strip() or not self.task_id.strip():
            raise ValueError("job_id and task_id must be non-empty")
        if self.interval_seconds is not None and self.interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive when provided")


@dataclass(frozen=True, slots=True)
class ResourceBudget:
    max_steps: int
    max_tool_calls: int
    max_runtime_seconds: float

    def __post_init__(self) -> None:
        if self.max_steps <= 0 or self.max_tool_calls <= 0 or self.max_runtime_seconds <= 0:
            raise ValueError("resource limits must be positive")

    def allows(self, *, steps: int, tool_calls: int, runtime_seconds: float) -> bool:
        return (
            steps <= self.max_steps
            and tool_calls <= self.max_tool_calls
            and runtime_seconds <= self.max_runtime_seconds
        )
