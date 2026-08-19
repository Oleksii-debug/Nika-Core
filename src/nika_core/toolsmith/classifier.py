from __future__ import annotations

from .contracts import CapabilityGap, GapDecision, GapDisposition, GapKind

_BLOCKING_KINDS = frozenset(
    {
        GapKind.MISSING_INFORMATION,
        GapKind.AMBIGUOUS_GOAL,
        GapKind.TOOL_FAILED,
        GapKind.MODEL_FAILED,
        GapKind.PERMISSION_DENIED,
    }
)


def classify_gap(gap: CapabilityGap) -> GapDecision:
    """Return a deterministic escalation decision without invoking a model.

    Only a genuine MISSING_CAPABILITY is eligible for building. Every evidence-quality,
    upstream-tool, model, or permission failure blocks capability construction instead of
    disguising an execution failure as a reason to self-modify.
    """

    if gap.kind is GapKind.EXISTING_CAPABILITY_AVAILABLE:
        return GapDecision(GapDisposition.REUSE, "existing capability is available")
    if gap.kind in _BLOCKING_KINDS:
        return GapDecision(GapDisposition.BLOCK, f"gap kind {gap.kind.value} cannot trigger build")
    if gap.kind is not GapKind.MISSING_CAPABILITY:
        return GapDecision(GapDisposition.BLOCK, "unsupported gap kind")
    if not gap.attempted_methods:
        return GapDecision(GapDisposition.BLOCK, "missing capability search evidence")
    if not gap.permission_ceiling:
        return GapDecision(GapDisposition.BLOCK, "missing task permission ceiling")
    return GapDecision(GapDisposition.BUILD, "capability is genuinely missing after deterministic search")
