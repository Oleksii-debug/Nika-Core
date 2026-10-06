from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class UICommand(BaseModel):
    """Validated command crossing from the local WebView into Python."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1, max_length=120)
    action_id: str = Field(min_length=3, pattern=r"^[a-z0-9_.-]+$")
    payload: dict[str, Any] = Field(default_factory=dict)


class UIResult(BaseModel):
    """Serializable response safe to expose through the pywebview facade."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str
    status: Literal["accepted", "completed", "rejected", "failed"]
    message: str = ""
    focus_id: str | None = None


class UIActionView(BaseModel):
    """User-facing action/keymap metadata exposed without handler objects."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action_id: str
    label: str
    category: str
    scope: str
    binding: str | None
    may_be_unbound: bool
