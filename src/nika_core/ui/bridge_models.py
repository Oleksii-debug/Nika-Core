from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class UICommand(BaseModel):
    """Validated command crossing from the local web UI into Python."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1, max_length=120)
    action_id: str = Field(min_length=3, pattern=r"^[a-z0-9_.-]+$")
    payload: dict[str, Any] = Field(default_factory=dict)


class UIResult(BaseModel):
    """Serializable response safe to expose through the pywebview facade."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str
    status: Literal["accepted", "completed", "rejected", "failed"]
    task_id: str | None = None
    message: str = ""


class UIEvent(BaseModel):
    """Text-first state event for status regions and activity logs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    kind: Literal["status", "task", "agent", "approval", "error"]
    message: str
    polite: bool = True
