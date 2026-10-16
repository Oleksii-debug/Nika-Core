from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from nika_core.plugins.sdk import CURRENT_PLUGIN_API, PluginManifest
from nika_core.tools import ToolRisk

_IDENTIFIER_PATTERN = r"^[a-z0-9][a-z0-9_.-]+$"


class WorkspaceCompatibilityError(ValueError):
    """Raised when a workspace cannot be activated against available plugins."""


class PluginRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plugin_id: Annotated[str, Field(min_length=3, pattern=_IDENTIFIER_PATTERN)]
    api_min: Annotated[int, Field(ge=1)] = CURRENT_PLUGIN_API
    api_max: Annotated[int, Field(ge=1)] = CURRENT_PLUGIN_API
    required_capabilities: tuple[str, ...] = ()
    required_permission_ids: tuple[
        Annotated[str, Field(min_length=3, pattern=_IDENTIFIER_PATTERN)], ...
    ] = ()
    required_action_ids: tuple[
        Annotated[str, Field(min_length=3, pattern=_IDENTIFIER_PATTERN)], ...
    ] = ()

    @field_validator(
        "required_capabilities",
        "required_permission_ids",
        "required_action_ids",
    )
    @classmethod
    def normalize_identifiers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(item.strip() for item in value if item.strip()))
        if len(normalized) != len(value):
            raise ValueError("workspace plugin requirements must be unique and non-blank")
        if any("." not in item for item in normalized):
            raise ValueError("workspace plugin IDs must be stable dotted identifiers")
        return normalized

    @model_validator(mode="after")
    def validate_api_range(self) -> PluginRequirement:
        if self.api_min > self.api_max:
            raise ValueError("api_min must not exceed api_max")
        return self


class WorkspaceCapabilityGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plugin_id: Annotated[str, Field(min_length=3, pattern=_IDENTIFIER_PATTERN)]
    capability_id: Annotated[str, Field(min_length=3, pattern=_IDENTIFIER_PATTERN)]
    max_risk: ToolRisk = ToolRisk.READ_ONLY


class WorkspaceManifest(BaseModel):
    """Portable workspace declaration with explicit dependencies and permission ceilings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    format_version: Literal[1] = 1
    workspace_id: Annotated[str, Field(min_length=3, pattern=_IDENTIFIER_PATTERN)]
    name: Annotated[str, Field(min_length=1, max_length=120)]
    version: Annotated[str, Field(min_length=1, max_length=64)]
    required_plugins: tuple[PluginRequirement, ...] = ()
    capability_grants: tuple[WorkspaceCapabilityGrant, ...] = ()
    permission_ids: tuple[
        Annotated[str, Field(min_length=3, pattern=_IDENTIFIER_PATTERN)], ...
    ] = ()
    action_ids: tuple[Annotated[str, Field(min_length=3, pattern=_IDENTIFIER_PATTERN)], ...] = ()
    data_roots: tuple[str, ...] = ()

    @field_validator("data_roots")
    @classmethod
    def normalize_roots(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(item.strip() for item in value if item.strip()))
        if len(normalized) != len(value):
            raise ValueError("workspace data roots must be unique and non-blank")
        return normalized

    @field_validator("permission_ids", "action_ids")
    @classmethod
    def normalize_identifiers(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(item.strip() for item in value if item.strip()))
        if len(normalized) != len(value):
            raise ValueError("workspace identifiers must be unique and non-blank")
        if any("." not in item for item in normalized):
            raise ValueError("workspace permission/action IDs must be stable dotted identifiers")
        return normalized

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


@dataclass(frozen=True, slots=True)
class WorkspacePolicyCatalog:
    permission_ids: frozenset[str] = frozenset()
    action_ids: frozenset[str] = frozenset()


class WorkspaceCatalog:
    """Validate workspace compatibility without importing or activating plugin code."""

    def __init__(self, policy: WorkspacePolicyCatalog | None = None) -> None:
        self._policy = policy or WorkspacePolicyCatalog()

    def validate(
        self,
        workspace: WorkspaceManifest,
        plugins: Mapping[str, PluginManifest],
    ) -> None:
        unknown_permissions = sorted(set(workspace.permission_ids) - self._policy.permission_ids)
        if unknown_permissions:
            raise WorkspaceCompatibilityError(
                "unknown workspace permission IDs: " + ", ".join(unknown_permissions)
            )
        unknown_actions = sorted(set(workspace.action_ids) - self._policy.action_ids)
        if unknown_actions:
            raise WorkspaceCompatibilityError(
                "unknown workspace Action Registry IDs: " + ", ".join(unknown_actions)
            )

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
            missing_permissions = sorted(
                set(requirement.required_permission_ids) - set(plugin.permission_ids)
            )
            if missing_permissions:
                raise WorkspaceCompatibilityError(
                    f"plugin {plugin_id} does not declare permissions: "
                    + ", ".join(missing_permissions)
                )
            unknown_required_permissions = sorted(
                set(requirement.required_permission_ids) - self._policy.permission_ids
            )
            if unknown_required_permissions:
                raise WorkspaceCompatibilityError(
                    "unknown workspace plugin permission IDs: "
                    + ", ".join(unknown_required_permissions)
                )
            missing_actions = sorted(
                set(requirement.required_action_ids) - set(plugin.action_ids)
            )
            if missing_actions:
                raise WorkspaceCompatibilityError(
                    f"plugin {plugin_id} does not declare Action Registry IDs: "
                    + ", ".join(missing_actions)
                )
            unknown_required_actions = sorted(
                set(requirement.required_action_ids) - self._policy.action_ids
            )
            if unknown_required_actions:
                raise WorkspaceCompatibilityError(
                    "unknown workspace plugin Action Registry IDs: "
                    + ", ".join(unknown_required_actions)
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

    def effective_permission_ids(self, workspace: WorkspaceManifest) -> tuple[str, ...]:
        selected = set(workspace.permission_ids)
        for requirement in workspace.required_plugins:
            selected.update(requirement.required_permission_ids)
        return tuple(sorted(selected))

    @staticmethod
    def high_impact_ids(
        workspace: WorkspaceManifest,
        plugins: Mapping[str, PluginManifest],
    ) -> tuple[str, ...]:
        high_impact: list[str] = []
        for grant in workspace.capability_grants:
            capability = plugins[grant.plugin_id].capability_map()[grant.capability_id]
            if capability.risk is ToolRisk.HIGH_IMPACT:
                high_impact.append(f"{grant.plugin_id}:{grant.capability_id}")
        return tuple(sorted(high_impact))
