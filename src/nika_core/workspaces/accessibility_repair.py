from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from nika_core.tools import ToolRisk
from .catalog import PluginRequirement, WorkspaceCapabilityGrant, WorkspaceManifest


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


@runtime_checkable
class AccessibilityFallbackPort(Protocol):
    """Optional lower-confidence vision/OCR fallback when semantic inspection is insufficient."""

    async def inspect_visual(self, target: str) -> AccessibilityEvidence: ...


class AccessibilityRepairService:
    """Enforce semantic DOM/UIA inspection before any visual fallback is consulted."""

    def __init__(
        self,
        semantic: AccessibilityInteractionPort,
        fallback: AccessibilityFallbackPort | None = None,
    ) -> None:
        self._semantic = semantic
        self._fallback = fallback

    async def inspect_browser(self, target: str) -> AccessibilityEvidence:
        evidence = await self._semantic.inspect_browser(target)
        return await self._maybe_fallback(target, evidence)

    async def inspect_windows(self, target: str) -> AccessibilityEvidence:
        evidence = await self._semantic.inspect_windows(target)
        return await self._maybe_fallback(target, evidence)

    async def _maybe_fallback(
        self,
        target: str,
        evidence: AccessibilityEvidence,
    ) -> AccessibilityEvidence:
        if evidence.accessible_controls or self._fallback is None:
            return evidence
        fallback = await self._fallback.inspect_visual(target)
        if fallback.method not in {EvidenceMethod.VISION, EvidenceMethod.COORDINATE}:
            raise ValueError("fallback adapter must return visual or coordinate provenance")
        return fallback
