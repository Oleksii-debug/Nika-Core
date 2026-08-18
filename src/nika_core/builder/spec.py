from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ToolGrant(BaseModel):
    """Declarative permission request for one registered Nika tool."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_id: Annotated[str, Field(min_length=3, pattern=r"^[a-z0-9_.-]+$")]
    max_risk: Annotated[int, Field(ge=0, le=4)] = 0
    scopes: tuple[str, ...] = ()

    @field_validator("scopes")
    @classmethod
    def normalize_scopes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(item.strip() for item in value if item.strip()))
        if any(len(item) > 160 for item in normalized):
            raise ValueError("tool scope is too long")
        return normalized


class AgentDefinition(BaseModel):
    """Versioned portable Agent Builder document independent from runtime implementation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format_version: Literal[1] = 1
    agent_id: Annotated[str, Field(min_length=3, pattern=r"^[a-z0-9][a-z0-9_.-]+$")]
    version: Annotated[int, Field(ge=1)] = 1
    name: Annotated[str, Field(min_length=1, max_length=120)]
    goal: Annotated[str, Field(min_length=1, max_length=4000)]
    instructions: Annotated[str, Field(min_length=1, max_length=40_000)]
    model_profile: Annotated[str, Field(min_length=1, max_length=120)] = "default"
    schedule_id: Annotated[str | None, Field(max_length=160)] = None
    resource_budget_ref: Annotated[str | None, Field(max_length=160)] = None
    tool_grants: tuple[ToolGrant, ...] = ()
    max_steps: Annotated[int, Field(ge=1, le=100_000)] = 100
    enabled: bool = True

    @field_validator("model_profile", "schedule_id", "resource_budget_ref")
    @classmethod
    def strip_reference(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("reference must not be blank")
        return normalized

    @model_validator(mode="after")
    def reject_duplicate_tools(self) -> AgentDefinition:
        ids = [grant.tool_id for grant in self.tool_grants]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate tool grants are not allowed")
        return self

    def export_json(self) -> str:
        return self.model_dump_json(indent=2)

    @classmethod
    def import_json(cls, payload: str) -> AgentDefinition:
        return cls.model_validate_json(payload)
