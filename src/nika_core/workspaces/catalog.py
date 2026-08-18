from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from nika_core.plugins.sdk import CURRENT_PLUGIN_API, PluginManifest
from nika_core.tools import ToolRisk


class WorkspaceCompatibilityError(ValueError):
    """Raised when a workspace cannot be activated against available plugins."""


class PluginRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plugin_id: Annotated[str, Field(min_length=3, pattern=r"^[a-z0-9][a-z0-9_.-]+$")]
    api_min: Annotated[int, Field(ge=1)] = CURRENT_PLUGIN_API
    api_max: Annotated[int, Field(ge=1)] = CURRENT_PLUGIN_API
    required_capabilities: tuple[str, ...] = ()

    @field_validator("required_capabilities")
    @classmethod
    def normalize_capabilities(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.strip() for item in value if item.strip()))

    @model_validator(mode="after")
    def validate_api_range(self) -> PluginRequirement:
        if self.api_min > self.api_max:
            raise ValueError("api_min must not exceed api_max")
        return self


class WorkspaceCapabilityGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plugin_id: Annotated[str, Field(min_length=3, pattern=r"^[a-z0-9][a-z0-9_.-]+$")]
    capability_id: Annotated[str, Field(min_length=3, pattern=r"^[a-z0-9][a-z0-9_.-]+$")]
    max_risk: ToolRisk = ToolRisk.READ_ONLY


class WorkspaceManifest(BaseModel):
    """Portable workspace declaration with explicit dependencies and permission ceilings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format_version: Literal[1] = 1
    workspace_id: Annotated[str, Field(min_length=3, pattern=r"^[a-z0-9][a-z0-9_.-]+$")]
    name: Annotated[str, Field(min_length=1, max_length=120)]
    version: Annotated[str, Field(min_length=1, max_length=64)]
    required_plugins: tuple[PluginRequirement, ...] = ()
    capability_grants: tuple[WorkspaceCapabilityGrant, ...] = ()
    data_roots: tuple[str, ...] = ()

    @field_validator("data_roots")
    @classmethod
    def normalize_roots(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(item.strip() for item in value if item.strip()))

    @model_validator(mode="after")
    def reject_duplicate_requirements_and_grants(self) -> WorkspaceManifest:
        plugin_ids = [item.plugin_id for item in self.required_plugins]
        if len(plugin_ids) != len(set(plugin_ids)):
            raise ValueError("duplicate required plugin")
        grant_ids = [(item.plugin_id, item.capability_id) for item in self.capability_grants]
        if len(grant_ids) != len(set(grant_ids)):
            raise ValueError("duplicate workspace capability grant")
        return self


class WorkspaceResolver:
    """Resolve workspace-relative paths while preventing traversal outside the root."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def resolve(self, relative_path: str) -> Path:
        candidate = (self.root / relative_path).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError("workspace path escapes configured root")
        return candidate


_RISK_ORDER = {
    ToolRisk.READ_ONLY: 0,
    ToolRisk.LOCAL_WRITE: 1,
    ToolRisk.EXTERNAL_SIDE_EFFECT: 2,
    ToolRisk.HIGH_IMPACT: 3,
}


class WorkspaceCatalog:
    """Validate workspace compatibility without importing or activating plugin code."""

    def validate(
        self,
        workspace: WorkspaceManifest,
        plugins: Mapping[str, PluginManifest],
    ) -> None:
        requirements = {item.plugin_id: item for item in workspace.required_plugins}
        for plugin_id, requirement in requirements.items():
            plugin = plugins.get(plugin_id)
            if plugin is None:
                raise WorkspaceCompatibilityError(f"missing required plugin: {plugin_id}")
            try:
                plugin.assert_compatible(CURRENT_PLUGIN_API)
            except ValueError as exc:
                raise WorkspaceCompatibilityError(str(exc)) from exc
            overlapping_min = max(requirement.api_min, plugin.plugin_api_min)
            overlapping_max = min(requirement.api_max, plugin.plugin_api_max)
            if overlapping_min > overlapping_max:
                raise WorkspaceCompatibilityError(f"incompatible plugin API: {plugin_id}")
            capabilities = plugin.capability_map()
            missing = [
                capability_id
                for capability_id in requirement.required_capabilities
                if capability_id not in capabilities
            ]
            if missing:
                raise WorkspaceCompatibilityError(
                    f"plugin {plugin_id} lacks capabilities: {', '.join(missing)}"
                )

        for grant in workspace.capability_grants:
            requirement = requirements.get(grant.plugin_id)
            if requirement is None:
                raise WorkspaceCompatibilityError(
                    f"capability grant references undeclared plugin: {grant.plugin_id}"
                )
            if grant.capability_id not in requirement.required_capabilities:
                raise WorkspaceCompatibilityError(
                    f"capability grant is not declared as required: {grant.capability_id}"
                )
            plugin = plugins[grant.plugin_id]
            capability = plugin.capability_map().get(grant.capability_id)
            if capability is None:
                raise WorkspaceCompatibilityError(
                    f"unknown capability {grant.plugin_id}:{grant.capability_id}"
                )
            if _RISK_ORDER[capability.risk] > _RISK_ORDER[grant.max_risk]:
                raise WorkspaceCompatibilityError(
                    f"capability risk exceeds workspace ceiling: {grant.capability_id}"
                )
