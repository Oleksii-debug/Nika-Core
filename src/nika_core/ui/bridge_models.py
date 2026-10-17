from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from nika_core.ui.payload_safety import validate_ui_payload


class UICommand(BaseModel):
    """Validated command crossing from the local WebView into Python."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(min_length=1, max_length=120)
    action_id: str = Field(min_length=3, pattern=r"^[a-z0-9_.-]+$")
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("request_id", mode="before")
    @classmethod
    def validate_request_id(cls, value: object) -> str:
        # A correlation ID is reflected to assistive status/log text. Never
        # admit multiline, bidi, control or behavioral string carriers.
        if (
            type(value) is not str
            or not 1 <= len(value) <= 120
            or not all(
                char.isascii() and (char.isalnum() or char in "-_.:")
                for char in value
            )
        ):
            raise ValueError("request_id must be a plain ASCII command token")
        return value

    @field_validator("payload", mode="before")
    @classmethod
    def validate_payload(cls, value: object) -> dict[str, Any]:
        return validate_ui_payload(value)


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
