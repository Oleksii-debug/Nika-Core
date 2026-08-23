from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol, runtime_checkable

from nika_core.tools import ToolRisk

from .catalog import PluginRequirement, WorkspaceCapabilityGrant, WorkspaceManifest

ACCESSIBILITY_REPAIR_SCHEMA = "nika-accessibility-repair-v2"
ACCESSIBILITY_HELPER_SCHEMA = "nika-accessibility-helper-v1"

ACCESSIBILITY_REPAIR_MANIFEST = WorkspaceManifest(
    workspace_id="accessibility.repair",
    name="Accessibility Repair",
    version="1.1.0",
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

_TOKEN_VALUE = re.compile(
    r"(?:"
    r"gh[pousr]_[A-Za-z0-9]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|"
    r"sk-[A-Za-z0-9_-]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|"
    r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
    r")"
)


class AccessibilityRepairError(ValueError):
    """Raised when accessibility evidence cannot satisfy fail-closed policy."""


class EvidenceMethod(StrEnum):
    DOM = "dom"
    UIA = "uia"
    OCR = "ocr"
    VISION = "vision"
    COORDINATE = "coordinate"


class FallbackCause(StrEnum):
    MISSING_CONTROLS = "missing_controls"
    LOW_CONFIDENCE = "low_confidence"
    AMBIGUOUS_TARGET = "ambiguous_target"
    ADAPTER_ERROR = "adapter_error"


@dataclass(frozen=True, slots=True)
class FallbackAttempt:
    method: EvidenceMethod
    cause: FallbackCause
    confidence: float
    target_revision: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.method, EvidenceMethod):
            raise AccessibilityRepairError("fallback attempt method must be EvidenceMethod")
        if not isinstance(self.cause, FallbackCause):
            raise AccessibilityRepairError("fallback attempt cause must be FallbackCause")
        if not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise AccessibilityRepairError("fallback attempt confidence must be finite in [0, 1]")
        _reject_token_shaped_text(self.target_revision, "fallback target_revision")


@dataclass(frozen=True, slots=True)
class AccessibilityEvidence:
    target: str
    method: EvidenceMethod
    summary: str
    accessible_controls: tuple[str, ...] = ()
    confidence: float = 1.0
    target_revision: str = ""
    candidate_count: int = 1
    redacted: bool = True
    contains_sensitive_data: bool = False
    fallback_attempts: tuple[FallbackAttempt, ...] = ()

    def __post_init__(self) -> None:
        if not self.target.strip() or not self.summary.strip():
            raise AccessibilityRepairError(
                "accessibility evidence target and summary must not be empty"
            )
        if not isinstance(self.method, EvidenceMethod):
            raise AccessibilityRepairError("accessibility evidence method must be EvidenceMethod")
        if not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise AccessibilityRepairError("confidence must be finite and between zero and one")
        if type(self.candidate_count) is not int or self.candidate_count < 1:
            raise AccessibilityRepairError("candidate_count must be an exact positive integer")
        if len(set(self.accessible_controls)) != len(self.accessible_controls):
            raise AccessibilityRepairError("accessible control names must be unique")
        if any(not item.strip() for item in self.accessible_controls):
            raise AccessibilityRepairError("accessible control names must not be empty")
        if self.contains_sensitive_data:
            raise AccessibilityRepairError(
                "accessibility evidence containing sensitive visual data must not cross the boundary"
            )
        if self.method in {EvidenceMethod.OCR, EvidenceMethod.VISION, EvidenceMethod.COORDINATE}:
            if self.confidence == 1.0:
                raise AccessibilityRepairError(
                    "fallback evidence must not claim perfect semantic confidence"
                )
            if not self.redacted:
                raise AccessibilityRepairError(
                    "visual fallback evidence must be redacted before persistence or logging"
                )
        for label, value in (
            ("target", self.target),
            ("summary", self.summary),
            ("target_revision", self.target_revision),
            ("accessible_controls", "\n".join(self.accessible_controls)),
        ):
            _reject_token_shaped_text(value, label)

    @property
    def ambiguous(self) -> bool:
        return self.candidate_count != 1

    @property
    def evidence_digest(self) -> str:
        payload = {
            "schema": ACCESSIBILITY_REPAIR_SCHEMA,
            "target": self.target,
            "method": self.method.value,
            "summary": self.summary,
            "accessible_controls": list(self.accessible_controls),
            "confidence": self.confidence,
            "target_revision": self.target_revision,
            "candidate_count": self.candidate_count,
            "fallback_attempts": [
                {
                    "method": attempt.method.value,
                    "cause": attempt.cause.value,
                    "confidence": attempt.confidence,
                    "target_revision": attempt.target_revision,
                }
                for attempt in self.fallback_attempts
            ],
        }
        return hashlib.sha256(_canonical(payload).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class SafeActionHandoff:
    target: str
    control_name: str
    method: EvidenceMethod
    target_revision: str
    evidence_digest: str
    confidence: float
    requires_approval: bool = True

    def __post_init__(self) -> None:
        if not self.target.strip() or not self.control_name.strip() or not self.target_revision.strip():
            raise AccessibilityRepairError(
                "safe action handoff requires target, control_name and target_revision"
            )
        if not _is_sha256(self.evidence_digest):
            raise AccessibilityRepairError("safe action handoff requires a SHA-256 evidence digest")
        if not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0:
            raise AccessibilityRepairError("safe action handoff confidence is invalid")
        if self.requires_approval is not True:
            raise AccessibilityRepairError("accessibility action handoff may not bypass approval")


@dataclass(frozen=True, slots=True)
class AccessibilityHelperSpec:
    helper_id: str
    target: str
    target_revision: str
    control_name: str
    evidence_method: EvidenceMethod
    evidence_digest: str
    schema_version: str = ACCESSIBILITY_HELPER_SCHEMA
    requires_approval: bool = True

    def __post_init__(self) -> None:
        if (
            not self.helper_id.strip()
            or not self.target.strip()
            or not self.target_revision.strip()
            or not self.control_name.strip()
        ):
            raise AccessibilityRepairError("accessibility helper identity must not be empty")
        if self.schema_version != ACCESSIBILITY_HELPER_SCHEMA:
            raise AccessibilityRepairError("unsupported accessibility helper schema")
        if self.evidence_method is EvidenceMethod.COORDINATE:
            raise AccessibilityRepairError(
                "repeatable accessibility helpers may not persist coordinate targeting"
            )
        if not _is_sha256(self.evidence_digest):
            raise AccessibilityRepairError("helper requires a SHA-256 evidence digest")
        if self.requires_approval is not True:
            raise AccessibilityRepairError("accessibility helper may not bypass approval")


@runtime_checkable
class AccessibilityInteractionPort(Protocol):
    """Semantic-first inspection boundary; action execution remains separately permissioned."""

    async def inspect_browser(self, target: str) -> AccessibilityEvidence: ...

    async def inspect_windows(self, target: str) -> AccessibilityEvidence: ...


@runtime_checkable
class AccessibilityFallbackPort(Protocol):
    """Read-only lower-tier inspection port used only after higher tiers fail policy."""

    async def inspect_visual(self, target: str) -> AccessibilityEvidence: ...


class AccessibilityRepairService:
    """DOM/UIA first, then OCR, vision, and explicitly configured coordinate-last fallback."""

    def __init__(
        self,
        semantic: AccessibilityInteractionPort,
        fallback: AccessibilityFallbackPort | None = None,
        *,
        ocr: AccessibilityFallbackPort | None = None,
        coordinate: AccessibilityFallbackPort | None = None,
        min_semantic_confidence: float = 0.80,
        min_fallback_confidence: float = 0.65,
    ) -> None:
        self._semantic = semantic
        self._vision = fallback
        self._ocr = ocr
        self._coordinate = coordinate
        self._min_semantic_confidence = _confidence_threshold(
            min_semantic_confidence, "min_semantic_confidence"
        )
        self._min_fallback_confidence = _confidence_threshold(
            min_fallback_confidence, "min_fallback_confidence"
        )

    async def inspect_browser(self, target: str) -> AccessibilityEvidence:
        evidence = await self._semantic.inspect_browser(target)
        return await self._resolve(target, evidence)

    async def inspect_windows(self, target: str) -> AccessibilityEvidence:
        evidence = await self._semantic.inspect_windows(target)
        return await self._resolve(target, evidence)

    def prepare_action_handoff(
        self,
        evidence: AccessibilityEvidence,
        *,
        control_name: str,
        expected_target_revision: str,
    ) -> SafeActionHandoff:
        self._require_actionable(evidence, self._threshold_for(evidence.method))
        if not expected_target_revision.strip():
            raise AccessibilityRepairError("expected_target_revision must not be empty")
        if evidence.target_revision != expected_target_revision:
            raise AccessibilityRepairError(
                "target revision changed after inspection; re-inspection is required"
            )
        if control_name not in evidence.accessible_controls:
            raise AccessibilityRepairError(
                "requested control is not present in the inspected accessibility evidence"
            )
        if evidence.method is EvidenceMethod.COORDINATE and not self._coordinate_chain_is_complete(
            evidence
        ):
            raise AccessibilityRepairError(
                "coordinate handoff requires recorded semantic, OCR and vision fallback failures"
            )
        return SafeActionHandoff(
            target=evidence.target,
            control_name=control_name,
            method=evidence.method,
            target_revision=evidence.target_revision,
            evidence_digest=evidence.evidence_digest,
            confidence=evidence.confidence,
        )

    def build_helper_spec(
        self,
        evidence: AccessibilityEvidence,
        *,
        control_name: str,
        expected_target_revision: str,
    ) -> AccessibilityHelperSpec:
        handoff = self.prepare_action_handoff(
            evidence,
            control_name=control_name,
            expected_target_revision=expected_target_revision,
        )
        if handoff.method is EvidenceMethod.COORDINATE:
            raise AccessibilityRepairError(
                "coordinate evidence is too brittle for a repeatable accessibility helper"
            )
        helper_seed = _canonical(
            {
                "schema": ACCESSIBILITY_HELPER_SCHEMA,
                "target": handoff.target,
                "target_revision": handoff.target_revision,
                "control_name": handoff.control_name,
                "method": handoff.method.value,
                "evidence_digest": handoff.evidence_digest,
            }
        )
        return AccessibilityHelperSpec(
            helper_id=f"accessibility-helper-{hashlib.sha256(helper_seed.encode()).hexdigest()[:24]}",
            target=handoff.target,
            target_revision=handoff.target_revision,
            control_name=handoff.control_name,
            evidence_method=handoff.method,
            evidence_digest=handoff.evidence_digest,
        )

    async def _resolve(
        self,
        target: str,
        semantic_evidence: AccessibilityEvidence,
    ) -> AccessibilityEvidence:
        if semantic_evidence.method not in {EvidenceMethod.DOM, EvidenceMethod.UIA}:
            raise AccessibilityRepairError("semantic adapter must return DOM or UIA evidence")
        if semantic_evidence.target != target:
            raise AccessibilityRepairError("semantic adapter returned evidence for another target")
        reason = self._failure_cause(
            semantic_evidence,
            threshold=self._min_semantic_confidence,
        )
        if reason is None:
            return semantic_evidence

        attempts = [self._attempt(semantic_evidence, reason)]
        tiers = (
            (self._ocr, EvidenceMethod.OCR),
            (self._vision, EvidenceMethod.VISION),
            (self._coordinate, EvidenceMethod.COORDINATE),
        )
        for port, expected_method in tiers:
            if port is None:
                continue
            if expected_method is EvidenceMethod.COORDINATE:
                prior_methods = {attempt.method for attempt in attempts}
                if not {EvidenceMethod.OCR, EvidenceMethod.VISION}.issubset(prior_methods):
                    continue
            try:
                candidate = await port.inspect_visual(target)
            except (OSError, RuntimeError):
                attempts.append(
                    FallbackAttempt(
                        method=expected_method,
                        cause=FallbackCause.ADAPTER_ERROR,
                        confidence=0.0,
                    )
                )
                if expected_method is EvidenceMethod.COORDINATE:
                    break
                continue
            if candidate.method is not expected_method:
                raise AccessibilityRepairError(
                    f"{expected_method.value} fallback adapter returned {candidate.method.value}"
                )
            if candidate.target != target:
                raise AccessibilityRepairError(
                    f"{expected_method.value} fallback adapter returned evidence for another target"
                )
            if candidate.target_revision != semantic_evidence.target_revision:
                raise AccessibilityRepairError(
                    "target revision changed during accessibility fallback; "
                    "semantic re-inspection is required"
                )
            candidate = replace(candidate, fallback_attempts=tuple(attempts))
            cause = self._failure_cause(
                candidate,
                threshold=self._min_fallback_confidence,
            )
            if cause is None:
                return candidate
            attempts.append(self._attempt(candidate, cause))

        reasons = ", ".join(f"{item.method.value}:{item.cause.value}" for item in attempts)
        raise AccessibilityRepairError(
            f"no trustworthy accessibility target could be resolved ({reasons})"
        )

    @staticmethod
    def _failure_cause(
        evidence: AccessibilityEvidence,
        *,
        threshold: float,
    ) -> FallbackCause | None:
        if evidence.ambiguous:
            return FallbackCause.AMBIGUOUS_TARGET
        if evidence.confidence < threshold:
            return FallbackCause.LOW_CONFIDENCE
        if not evidence.accessible_controls:
            return FallbackCause.MISSING_CONTROLS
        return None

    @staticmethod
    def _attempt(evidence: AccessibilityEvidence, cause: FallbackCause) -> FallbackAttempt:
        return FallbackAttempt(
            method=evidence.method,
            cause=cause,
            confidence=evidence.confidence,
            target_revision=evidence.target_revision,
        )

    def _require_actionable(self, evidence: AccessibilityEvidence, threshold: float) -> None:
        cause = self._failure_cause(evidence, threshold=threshold)
        if cause is not None:
            raise AccessibilityRepairError(
                f"accessibility evidence is not actionable: {cause.value}"
            )
        if not evidence.target_revision.strip():
            raise AccessibilityRepairError(
                "actionable accessibility evidence requires a target revision"
            )

    def _threshold_for(self, method: EvidenceMethod) -> float:
        if method in {EvidenceMethod.DOM, EvidenceMethod.UIA}:
            return self._min_semantic_confidence
        return self._min_fallback_confidence

    @staticmethod
    def _coordinate_chain_is_complete(evidence: AccessibilityEvidence) -> bool:
        prior_methods = {attempt.method for attempt in evidence.fallback_attempts}
        return {
            EvidenceMethod.DOM,
            EvidenceMethod.OCR,
            EvidenceMethod.VISION,
        }.issubset(prior_methods) or {
            EvidenceMethod.UIA,
            EvidenceMethod.OCR,
            EvidenceMethod.VISION,
        }.issubset(prior_methods)


def _confidence_threshold(value: float, label: str) -> float:
    if not math.isfinite(value) or not 0.0 < value < 1.0:
        raise AccessibilityRepairError(f"{label} must be finite and strictly between zero and one")
    return value


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def _reject_token_shaped_text(value: str, label: str) -> None:
    if value and _TOKEN_VALUE.search(value):
        raise AccessibilityRepairError(
            f"token-shaped credential material is forbidden in accessibility {label}"
        )
