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
        self.calls = 0

    async def inspect_browser(self, target: str) -> AccessibilityEvidence:
        self.calls += 1
        return self.evidence

    async def inspect_windows(self, target: str) -> AccessibilityEvidence:
        self.calls += 1
        return self.evidence


class Fallback:
    def __init__(self, evidence: AccessibilityEvidence) -> None:
        self.evidence = evidence
        self.calls = 0

    async def inspect_visual(self, target: str) -> AccessibilityEvidence:
        self.calls += 1
        return self.evidence


def _evidence(
    method: EvidenceMethod,
    *,
    revision: str,
    controls: tuple[str, ...],
    confidence: float = 0.9,
) -> AccessibilityEvidence:
    return AccessibilityEvidence(
        target="app://settings",
        method=method,
        summary=f"{method.value} observation",
        accessible_controls=controls,
        confidence=confidence,
        target_revision=revision,
    )


def test_fallback_may_not_cross_ui_revision_without_restarting_semantic_inspection() -> None:
    semantic = Semantic(
        _evidence(
            EvidenceMethod.DOM,
            revision="ui-v1",
            controls=(),
        )
    )
    ocr = Fallback(
        _evidence(
            EvidenceMethod.OCR,
            revision="ui-v2",
            controls=("Open",),
            confidence=0.85,
        )
    )
    service = AccessibilityRepairService(semantic, ocr=ocr)

    with pytest.raises(AccessibilityRepairError, match="revision|re-inspection|reinspection"):
        asyncio.run(service.inspect_browser("app://settings"))

    assert semantic.calls == 1
    assert ocr.calls == 1


def test_action_handoff_cannot_launder_a_cross_revision_fallback_chain() -> None:
    semantic = Semantic(
        _evidence(
            EvidenceMethod.UIA,
            revision="uia-v1",
            controls=(),
        )
    )
    vision = Fallback(
        _evidence(
            EvidenceMethod.VISION,
            revision="uia-v2",
            controls=("Apply",),
            confidence=0.85,
        )
    )
    service = AccessibilityRepairService(semantic, fallback=vision)

    with pytest.raises(AccessibilityRepairError, match="revision|re-inspection|reinspection"):
        resolved = asyncio.run(service.inspect_windows("app://settings"))
        service.prepare_action_handoff(
            resolved,
            control_name="Apply",
            expected_target_revision="uia-v2",
        )
