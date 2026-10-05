from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from nika_core.product_command.reference_safety import safe_evidence_reference


class CommandRouteKind(StrEnum):
    AGENT_TASK = "agent_task"
    TOOLSMITH = "toolsmith"
    PRODUCT_PROJECT = "product_project"
    AMBIGUOUS = "ambiguous"


class ProductStatusKind(StrEnum):
    REQUIREMENT = "requirement"
    MILESTONE = "milestone"
    ARCHITECTURE_DECISION = "architecture_decision"
    TEAM_ROLE = "team_role"
    REPOSITORY = "repository"
    COMPONENT = "component"
    QA = "qa"
    BUILD = "build"
    RELEASE = "release"
    CREDENTIAL = "credential"
    DEPLOYMENT = "deployment"
    INCIDENT = "incident"
    BLOCKER = "blocker"


class EvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = Field(min_length=1, max_length=64)
    reference: str = Field(min_length=1, max_length=512)
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    label: str = Field(min_length=1, max_length=240)

    @field_validator("reference", mode="before")
    @classmethod
    def sanitize_reference(cls, value: object) -> object:
        if isinstance(value, str):
            return safe_evidence_reference(value)
        return value


class ProductStatusEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ProductStatusKind
    item_id: str = Field(min_length=1, max_length=160)
    label: str = Field(min_length=1, max_length=240)
    state: str = Field(min_length=1, max_length=80)
    owner: str | None = Field(default=None, max_length=160)
    detail: str = Field(default="", max_length=4000)
    evidence: tuple[EvidenceReference, ...] = ()


class ProductUserDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=240)
    question: str = Field(min_length=1, max_length=4000)
    risk_level: int = Field(ge=0, le=4)
    state: str = Field(pattern=r"^(pending|approved|rejected|superseded)$")
    evidence: tuple[EvidenceReference, ...] = ()


class ProductProjectSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    project_id: str = Field(min_length=1, max_length=160)
    version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=240)
    goal: str = Field(min_length=1, max_length=4000)
    state: str = Field(min_length=1, max_length=80)
    updated_at: datetime
    current_decision: ProductUserDecision | None = None
    blocker_count: int = Field(default=0, ge=0)


class ProductProjectDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: ProductProjectSummary
    statuses: tuple[ProductStatusEntry, ...] = ()
    decisions: tuple[ProductUserDecision, ...] = ()
    logs: tuple[str, ...] = Field(default=(), max_length=200)
    errors: tuple[str, ...] = Field(default=(), max_length=100)


class CommandRouteDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    route: CommandRouteKind
    reason: str = Field(min_length=1, max_length=1000)
    requires_user_decision: bool = False
    project_id: str | None = Field(default=None, max_length=160)
    normalized_goal: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def validate_route(self) -> CommandRouteDecision:
        if self.route == CommandRouteKind.AMBIGUOUS and not self.requires_user_decision:
            raise ValueError("ambiguous command routing must require a user decision")
        if self.route != CommandRouteKind.PRODUCT_PROJECT and self.project_id is not None:
            raise ValueError("only ProductProject routing may carry project_id")
        return self
