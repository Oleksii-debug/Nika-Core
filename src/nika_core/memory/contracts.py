from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class MemoryScope(StrEnum):
    SHORT_TERM = "short_term"
    TASK = "task"
    THREAD = "thread"
    AGENT = "agent"
    WORKSPACE = "workspace"
    USER = "user"
    USER_LONG_TERM = "user"


@dataclass(frozen=True, slots=True)
class MemoryRetentionPolicy:
    ttl_seconds: int | None = None
    max_records: int | None = None

    def __post_init__(self) -> None:
        if self.ttl_seconds is not None and (
            isinstance(self.ttl_seconds, bool)
            or not isinstance(self.ttl_seconds, int)
            or self.ttl_seconds <= 0
        ):
            raise ValueError("ttl_seconds must be a positive integer or None")
        if self.max_records is not None and (
            isinstance(self.max_records, bool)
            or not isinstance(self.max_records, int)
            or self.max_records <= 0
        ):
            raise ValueError("max_records must be a positive integer or None")


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    scope: MemoryScope
    owner_id: str
    namespace: str
    key: str
    value: Any
    user_approved: bool
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime
