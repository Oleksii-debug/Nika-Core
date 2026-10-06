from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class MemoryScope(StrEnum):
    TASK = "task"
    AGENT = "agent"
    WORKSPACE = "workspace"
    USER = "user"


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
