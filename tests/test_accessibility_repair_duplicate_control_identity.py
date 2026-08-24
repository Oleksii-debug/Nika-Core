from __future__ import annotations

import asyncio

import pytest

from nika_core.workspaces.accessibility_repair import (
    AccessibilityEvidence,
    AccessibilityRepairError,
    AccessibilityRepairService,
    EvidenceMethod,
)


class Semantic:
    def __init__(self, evidence: AccessibilityEvidence) -> None:
        self.evidence = evidence

    async def inspect_browser(self, target: str) -> AccessibilityEvidence:
        return self.evidence

    async def inspect_windows(self, target: str) -> AccessibilityEvidence:
        return self.evidence


def _uia_evidence(controls: tuple[str, ...]) -> AccessibilityEvidence:
    return AccessibilityEvidence(
        target="app://settings",
        method=EvidenceMethod.UIA,
        summary="process-scoped UIA snapshot",
        accessible_controls=controls,
        confidence=1.0,
        target_revision="uia-tree-44",
    )


def test_duplicate_structural_snapshot_keeps_unique_named_control_actionable() -> None:
    snapshot = _uia_evidence(("Pane", "Pane", "Button:Repair now"))
    service = AccessibilityRepairService(Semantic(snapshot))

    resolved = asyncio.run(service.inspect_windows("app://settings"))
    handoff = service.prepare_action_handoff(
        resolved,
        control_name="Button:Repair now",
        expected_target_revision="uia-tree-44",
    )

    assert resolved.accessible_controls == ("Pane", "Pane", "Button:Repair now")
    assert handoff.control_name == "Button:Repair now"
    assert handoff.requires_approval is True


def test_duplicate_exact_control_identity_fails_closed_before_action_or_helper_handoff() -> None:
    snapshot = _uia_evidence(("Button:Repair now", "Button:Repair now"))
    service = AccessibilityRepairService(Semantic(snapshot))

    resolved = asyncio.run(service.inspect_windows("app://settings"))

    with pytest.raises(AccessibilityRepairError, match="identity is ambiguous"):
        service.prepare_action_handoff(
            resolved,
            control_name="Button:Repair now",
            expected_target_revision="uia-tree-44",
        )

    with pytest.raises(AccessibilityRepairError, match="identity is ambiguous"):
        service.build_helper_spec(
            resolved,
            control_name="Button:Repair now",
            expected_target_revision="uia-tree-44",
        )
