from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from nika_core.tools import ToolRisk
from nika_core.workspaces.catalog import PluginRequirement, WorkspaceCapabilityGrant, WorkspaceManifest


ACCESSIBILITY_REPAIR_MANIFEST = WorkspaceManifest(
    workspace_id="accessibility.repair",
    name="Accessibility Repair",
    version="1.0.0",
    required_plugins=(
        PluginRequirement(
            plugin_id="interaction.semantic",
            required_capabilities=("browser.dom.inspect", "windows.uia.inspect"),
        ),
    ),
    capability_grants=(
        WorkspaceCapabilityGrant(
            plugin_id="interaction.semantic",
            capability_id="browser.dom.inspect",
            max_risk=ToolRisk.READ_ONLY,
        ),
        WorkspaceCapabilityGrant(
            plugin_id="interaction.semantic",
            capability_id="windows.uia.inspect",
            max_risk=ToolRisk.READ_ONLY,
        ),
    ),
    data_roots=("artifacts",),
)


class EvidenceMethod(StrEnum):
    DOM = "dom"
    UIA = "uia"
    VISION = "vision"
    COORDINATE = "coordinate"


@dataclass(frozen=True, slots=True)
class AccessibilityEvidence:
    target: str
    method: EvidenceMethod
    summary: str
    accessible_controls: tuple[str, ...] = ()
    confidence: float = 1.0

    def __post_init__(self) -> None:
        if not self.target.strip() or not self.summary.strip():
            raise ValueError("accessibility evidence target and summary must not be empty")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("confidence must be between zero and one")
        if self.method in {EvidenceMethod.VISION, EvidenceMethod.COORDINATE} and self.confidence == 1.0:
            raise ValueError("fallback evidence must not claim perfect semantic confidence")


@runtime_checkable
class AccessibilityInteractionPort(Protocol):
    """Semantic-first inspection boundary; action execution remains separately permissioned."""

    async def inspect_browser(self, target: str) -> AccessibilityEvidence: ...

    async def inspect_windows(self, target: str) -> AccessibilityEvidence: ...
