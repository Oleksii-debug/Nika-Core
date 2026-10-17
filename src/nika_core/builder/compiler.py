from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from nika_core.builder.spec import AgentDefinition
from nika_core.tools import ToolRisk, ToolSpec


class RiskTier(IntEnum):
    R0_READ_ONLY = 0
    R1_LOCAL_REVERSIBLE = 1
    R2_EXTERNAL_WRITE = 2
    R3_SENSITIVE = 3
    R4_HIGH_IMPACT = 4


_TOOL_RISK_TO_TIER = {
    ToolRisk.READ_ONLY: RiskTier.R0_READ_ONLY,
    ToolRisk.LOCAL_WRITE: RiskTier.R1_LOCAL_REVERSIBLE,
    ToolRisk.EXTERNAL_SIDE_EFFECT: RiskTier.R2_EXTERNAL_WRITE,
    ToolRisk.HIGH_IMPACT: RiskTier.R4_HIGH_IMPACT,
}


@dataclass(frozen=True, slots=True)
class CompilationResult:
    definition: AgentDefinition
    required_human_approvals: tuple[str, ...]
    highest_risk: RiskTier

    @property
    def requires_human_approval(self) -> bool:
        return bool(self.required_human_approvals)


class AgentCompiler:
    """Deterministically validates a draft against current Nika registries."""

    def __init__(
        self,
        *,
        tools: tuple[ToolSpec, ...],
        model_profiles: set[str] | frozenset[str],
        schedule_ids: set[str] | frozenset[str] = frozenset(),
        resource_budget_refs: set[str] | frozenset[str] = frozenset(),
    ) -> None:
        # The registry is permission authority: never silently overwrite a duplicate.
        # Snapshot classifications so later caller mutations cannot change this compiler.
        self._tools: dict[str, RiskTier] = {}
        for tool in tools:
            if tool.tool_id in self._tools:
                raise ValueError("duplicate registered tool identity")
            self._tools[tool.tool_id] = _TOOL_RISK_TO_TIER[tool.risk]
        self._model_profiles = frozenset(model_profiles)
        self._schedule_ids = frozenset(schedule_ids)
        self._resource_budget_refs = frozenset(resource_budget_refs)

    def compile(self, definition: AgentDefinition) -> CompilationResult:
        # Pydantic's model_copy(update=...) and frozen-object mutation bypass validators.
        # Re-admit the full document before reviewing any tool or budget authority.
        # model_validate(existing_instance) alone would not revalidate by default.
        definition = AgentDefinition.model_validate(definition.model_dump(mode="python"))
        if definition.model_profile not in self._model_profiles:
            raise ValueError(f"unknown model profile: {definition.model_profile}")
        if definition.schedule_id is not None and definition.schedule_id not in self._schedule_ids:
            raise ValueError(f"unknown schedule: {definition.schedule_id}")
        if (
            definition.resource_budget_ref is not None
            and definition.resource_budget_ref not in self._resource_budget_refs
        ):
            raise ValueError(f"unknown resource budget: {definition.resource_budget_ref}")

        approvals: list[str] = []
        highest = RiskTier.R0_READ_ONLY
        for grant in definition.tool_grants:
            actual = self._tools.get(grant.tool_id)
            if actual is None:
                raise ValueError(f"unknown tool: {grant.tool_id}")
            declared = RiskTier(grant.max_risk)
            if declared < actual:
                raise ValueError(
                    f"tool grant for {grant.tool_id} permits {declared.name} but tool requires {actual.name}"
                )
            if declared > actual:
                raise ValueError(
                    f"tool grant for {grant.tool_id} overstates risk beyond registered tool classification"
                )
            highest = max(highest, actual)
            if actual is RiskTier.R4_HIGH_IMPACT:
                approvals.append(grant.tool_id)

        return CompilationResult(
            definition=definition.model_copy(deep=True),
            required_human_approvals=tuple(sorted(approvals)),
            highest_risk=highest,
        )
