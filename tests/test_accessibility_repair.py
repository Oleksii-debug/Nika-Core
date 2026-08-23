from __future__ import annotations

import asyncio

import pytest

from nika_core.workspaces.accessibility_repair import (
    AccessibilityEvidence,
    AccessibilityHelperSpec,
    AccessibilityRepairError,
    AccessibilityRepairService,
    EvidenceMethod,
    FallbackCause,
)


class Semantic:
    def __init__(self, evidence: AccessibilityEvidence) -> None:
        self.evidence = evidence

    async def inspect_browser(self, target: str) -> AccessibilityEvidence:
        return self.evidence

    async def inspect_windows(self, target: str) -> AccessibilityEvidence:
        return self.evidence


class Fallback:
    def __init__(self, evidence: AccessibilityEvidence) -> None:
        self.evidence = evidence
        self.calls = 0

    async def inspect_visual(self, target: str) -> AccessibilityEvidence:
        self.calls += 1
        return self.evidence


class FailingFallback:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    async def inspect_visual(self, target: str) -> AccessibilityEvidence:
        self.calls += 1
        raise self.error


def evidence(
    method: EvidenceMethod,
    *,
    controls: tuple[str, ...] = ("Open",),
    confidence: float = 0.9,
    revision: str = "ui-v1",
    candidates: int = 1,
) -> AccessibilityEvidence:
    return AccessibilityEvidence(
        target="app://settings",
        method=method,
        summary=f"{method.value} inspection",
        accessible_controls=controls,
        confidence=confidence,
        target_revision=revision,
        candidate_count=candidates,
    )


def test_semantic_evidence_prevents_lower_tier_calls() -> None:
    vision = Fallback(evidence(EvidenceMethod.VISION, confidence=0.8))
    service = AccessibilityRepairService(
        Semantic(evidence(EvidenceMethod.DOM)),
        fallback=vision,
    )

    resolved = asyncio.run(service.inspect_browser("app://settings"))

    assert resolved.method is EvidenceMethod.DOM
    assert vision.calls == 0
    assert resolved.fallback_attempts == ()


def test_ocr_precedes_vision_and_records_semantic_failure() -> None:
    ocr = Fallback(evidence(EvidenceMethod.OCR, confidence=0.78))
    vision = Fallback(evidence(EvidenceMethod.VISION, confidence=0.82))
    service = AccessibilityRepairService(
        Semantic(evidence(EvidenceMethod.DOM, controls=())),
        fallback=vision,
        ocr=ocr,
    )

    resolved = asyncio.run(service.inspect_browser("app://settings"))

    assert resolved.method is EvidenceMethod.OCR
    assert ocr.calls == 1
    assert vision.calls == 0
    assert len(resolved.fallback_attempts) == 1
    assert resolved.fallback_attempts[0].method is EvidenceMethod.DOM
    assert resolved.fallback_attempts[0].cause is FallbackCause.MISSING_CONTROLS


def test_operational_fallback_error_is_recorded_and_next_tier_can_recover() -> None:
    ocr = FailingFallback(RuntimeError("OCR adapter unavailable"))
    vision = Fallback(evidence(EvidenceMethod.VISION, confidence=0.82))
    service = AccessibilityRepairService(
        Semantic(evidence(EvidenceMethod.DOM, controls=())),
        fallback=vision,
        ocr=ocr,
    )

    resolved = asyncio.run(service.inspect_browser("app://settings"))

    assert resolved.method is EvidenceMethod.VISION
    assert ocr.calls == 1
    assert vision.calls == 1
    assert [(item.method, item.cause) for item in resolved.fallback_attempts] == [
        (EvidenceMethod.DOM, FallbackCause.MISSING_CONTROLS),
        (EvidenceMethod.OCR, FallbackCause.ADAPTER_ERROR),
    ]


def test_programming_error_from_fallback_is_not_masked_as_unavailable_tier() -> None:
    ocr = FailingFallback(TypeError("adapter contract bug"))
    service = AccessibilityRepairService(
        Semantic(evidence(EvidenceMethod.DOM, controls=())),
        ocr=ocr,
    )

    with pytest.raises(TypeError, match="adapter contract bug"):
        asyncio.run(service.inspect_browser("app://settings"))


def test_ambiguous_ocr_is_rejected_and_vision_can_recover() -> None:
    ocr = Fallback(evidence(EvidenceMethod.OCR, candidates=2, confidence=0.85))
    vision = Fallback(evidence(EvidenceMethod.VISION, confidence=0.82))
    service = AccessibilityRepairService(
        Semantic(evidence(EvidenceMethod.UIA, controls=())),
        fallback=vision,
        ocr=ocr,
    )

    resolved = asyncio.run(service.inspect_windows("app://settings"))

    assert resolved.method is EvidenceMethod.VISION
    assert [item.cause for item in resolved.fallback_attempts] == [
        FallbackCause.MISSING_CONTROLS,
        FallbackCause.AMBIGUOUS_TARGET,
    ]


def test_low_confidence_fallbacks_fail_closed() -> None:
    ocr = Fallback(evidence(EvidenceMethod.OCR, confidence=0.40))
    vision = Fallback(evidence(EvidenceMethod.VISION, confidence=0.50))
    service = AccessibilityRepairService(
        Semantic(evidence(EvidenceMethod.DOM, controls=())),
        fallback=vision,
        ocr=ocr,
    )

    with pytest.raises(AccessibilityRepairError, match="no trustworthy accessibility target"):
        asyncio.run(service.inspect_browser("app://settings"))


def test_coordinate_is_only_consulted_after_ocr_and_vision_failures() -> None:
    ocr = Fallback(evidence(EvidenceMethod.OCR, confidence=0.40))
    vision = Fallback(evidence(EvidenceMethod.VISION, candidates=2, confidence=0.80))
    coordinate = Fallback(evidence(EvidenceMethod.COORDINATE, confidence=0.75))
    service = AccessibilityRepairService(
        Semantic(evidence(EvidenceMethod.UIA, controls=())),
        fallback=vision,
        ocr=ocr,
        coordinate=coordinate,
    )

    resolved = asyncio.run(service.inspect_windows("app://settings"))

    assert resolved.method is EvidenceMethod.COORDINATE
    assert [item.method for item in resolved.fallback_attempts] == [
        EvidenceMethod.UIA,
        EvidenceMethod.OCR,
        EvidenceMethod.VISION,
    ]
    handoff = service.prepare_action_handoff(
        resolved,
        control_name="Open",
        expected_target_revision="ui-v1",
    )
    assert handoff.requires_approval is True
    with pytest.raises(AccessibilityRepairError, match="too brittle"):
        service.build_helper_spec(
            resolved,
            control_name="Open",
            expected_target_revision="ui-v1",
        )


def test_coordinate_is_not_used_when_higher_fallback_tiers_are_unavailable() -> None:
    coordinate = Fallback(evidence(EvidenceMethod.COORDINATE, confidence=0.75))
    service = AccessibilityRepairService(
        Semantic(evidence(EvidenceMethod.DOM, controls=())),
        coordinate=coordinate,
    )

    with pytest.raises(AccessibilityRepairError, match="no trustworthy accessibility target"):
        asyncio.run(service.inspect_browser("app://settings"))

    assert coordinate.calls == 0


def test_changed_ui_revision_rejects_action_handoff() -> None:
    resolved = evidence(EvidenceMethod.DOM, revision="ui-v2")
    service = AccessibilityRepairService(Semantic(resolved))

    with pytest.raises(AccessibilityRepairError, match="target revision changed"):
        service.prepare_action_handoff(
            resolved,
            control_name="Open",
            expected_target_revision="ui-v1",
        )


def test_safe_action_handoff_and_helper_are_deterministic_and_approval_bound() -> None:
    resolved = evidence(EvidenceMethod.UIA, controls=("Open", "Cancel"), revision="uia-tree-44")
    service = AccessibilityRepairService(Semantic(resolved))

    first = service.build_helper_spec(
        resolved,
        control_name="Open",
        expected_target_revision="uia-tree-44",
    )
    second = service.build_helper_spec(
        resolved,
        control_name="Open",
        expected_target_revision="uia-tree-44",
    )

    assert isinstance(first, AccessibilityHelperSpec)
    assert first == second
    assert first.requires_approval is True
    assert first.evidence_method is EvidenceMethod.UIA
    assert len(first.evidence_digest) == 64


def test_token_shaped_credential_material_is_rejected_before_logging() -> None:
    with pytest.raises(AccessibilityRepairError, match="credential material"):
        AccessibilityEvidence(
            target="app://settings",
            method=EvidenceMethod.VISION,
            summary="visible token sk-abcdefghijklmnopqrstuvwxyz012345",
            accessible_controls=("Open",),
            confidence=0.8,
            target_revision="ui-v1",
        )


def test_visual_evidence_requires_redaction() -> None:
    with pytest.raises(AccessibilityRepairError, match="redacted"):
        AccessibilityEvidence(
            target="app://settings",
            method=EvidenceMethod.OCR,
            summary="ocr",
            accessible_controls=("Open",),
            confidence=0.8,
            target_revision="ui-v1",
            redacted=False,
        )


def test_boolean_candidate_count_is_rejected() -> None:
    with pytest.raises(AccessibilityRepairError, match="exact positive integer"):
        AccessibilityEvidence(
            target="app://settings",
            method=EvidenceMethod.DOM,
            summary="dom",
            candidate_count=True,
        )
