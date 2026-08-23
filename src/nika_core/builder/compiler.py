from __future__ import annotations

from collections.abc import Collection, Mapping
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
        permission_catalog: Mapping[str, Collection[str]] | None = None,
    ) -> None:
        self._tools = {tool.tool_id: tool for tool in tools}
        self._model_profiles = frozenset(model_profiles)
        self._schedule_ids = frozenset(schedule_ids)
        self._resource_budget_refs = frozenset(resource_budget_refs)
        self._permission_catalog = {
            tool_id: frozenset(permission.strip() for permission in permissions if permission.strip())
            for tool_id, permissions in (permission_catalog or {}).items()
        }
        unknown_catalog_tools = sorted(set(self._permission_catalog) - set(self._tools))
        if unknown_catalog_tools:
            raise ValueError(
                "permission catalog references unknown tools: " + ", ".join(unknown_catalog_tools)
            )

    def compile(self, definition: AgentDefinition) -> CompilationResult:
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
            spec = self._tools.get(grant.tool_id)
            if spec is None:
                raise ValueError(f"unknown tool: {grant.tool_id}")
            actual = _TOOL_RISK_TO_TIER[spec.risk]
            declared = RiskTier(grant.max_risk)
            if declared < actual:
                raise ValueError(
                    f"tool grant for {grant.tool_id} permits {declared.name} but tool requires {actual.name}"
                )
            if declared > actual:
                raise ValueError(
                    f"tool grant for {grant.tool_id} overstates risk beyond registered tool classification"
                )

            allowed_permissions = self._permission_catalog.get(grant.tool_id, frozenset())
            unknown_permissions = sorted(set(grant.scopes) - allowed_permissions)
            if unknown_permissions:
                raise ValueError(
                    f"unknown permission scope(s) for {grant.tool_id}: "
                    + ", ".join(unknown_permissions)
                )

            highest = max(highest, actual)
            if actual is RiskTier.R4_HIGH_IMPACT:
                approvals.append(grant.tool_id)

        return CompilationResult(
            definition=definition,
            required_human_approvals=tuple(sorted(approvals)),
            highest_risk=highest,
        )
