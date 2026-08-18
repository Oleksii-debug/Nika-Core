from __future__ import annotations

from enum import IntEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class RiskLevel(IntEnum):
    """Approval boundary for actions exposed to a configurable agent."""

    R0_READ_ONLY = 0
    R1_REVERSIBLE = 1
    R2_EXTERNAL_WRITE = 2
    R3_SENSITIVE = 3
    R4_EXPLICIT_HUMAN = 4


class ToolGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_id: Annotated[str, Field(min_length=3, pattern=r"^[a-z0-9_.-]+$")]
    max_risk: RiskLevel = RiskLevel.R0_READ_ONLY
    scopes: tuple[str, ...] = ()

    @field_validator("scopes")
    @classmethod
    def normalize_scopes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(dict.fromkeys(scope.strip() for scope in value if scope.strip()))
        return cleaned


class PermissionPolicy(BaseModel):
    """Fail-closed permissions for user-created agents.

    R4 operations are never pre-authorized by policy. They require an explicit
    human approval token at execution time, outside this model.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    default_risk: RiskLevel = RiskLevel.R0_READ_ONLY
    tool_grants: tuple[ToolGrant, ...] = ()
    allow_network: bool = False
    allow_filesystem_write: bool = False
    allow_process_launch: bool = False

    @model_validator(mode="after")
    def reject_r4_pre_authorization(self) -> "PermissionPolicy":
        if self.default_risk >= RiskLevel.R4_EXPLICIT_HUMAN:
            raise ValueError("R4 cannot be pre-authorized")
        if any(grant.max_risk >= RiskLevel.R4_EXPLICIT_HUMAN for grant in self.tool_grants):
            raise ValueError("R4 tool grants require explicit human approval at execution time")
        return self

    def grant_for(self, tool_id: str) -> ToolGrant | None:
        return next((grant for grant in self.tool_grants if grant.tool_id == tool_id), None)

    def permits(self, tool_id: str, risk: RiskLevel) -> bool:
        if risk >= RiskLevel.R4_EXPLICIT_HUMAN:
            return False
        grant = self.grant_for(tool_id)
        return grant is not None and risk <= grant.max_risk


class AgentSpec(BaseModel):
    """Portable Agent Builder document, independent from runtime implementation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format_version: Literal[1] = 1
    agent_id: Annotated[str, Field(min_length=3, pattern=r"^[a-z0-9][a-z0-9_.-]+$")]
    name: Annotated[str, Field(min_length=1, max_length=120)]
    description: Annotated[str, Field(max_length=1000)] = ""
    system_prompt: Annotated[str, Field(min_length=1, max_length=40_000)]
    model_profile: Annotated[str, Field(min_length=1, max_length=120)] = "default"
    permission_policy: PermissionPolicy = Field(default_factory=PermissionPolicy)
    enabled: bool = True

    def export_json(self) -> str:
        return self.model_dump_json(indent=2)

    @classmethod
    def import_json(cls, payload: str) -> "AgentSpec":
        return cls.model_validate_json(payload)
