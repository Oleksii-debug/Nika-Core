from __future__ import annotations

from .contracts import CapabilityGap, GapDecision, GapDisposition, GapKind
from .reuse_search import ReuseSearchResult

_BLOCKING_KINDS = frozenset(
    {
        GapKind.MISSING_INFORMATION,
        GapKind.AMBIGUOUS_GOAL,
        GapKind.TOOL_FAILED,
        GapKind.MODEL_FAILED,
        GapKind.PERMISSION_DENIED,
    }
)


def classify_gap(
    gap: CapabilityGap,
    *,
    search_result: ReuseSearchResult | None = None,
) -> GapDecision:
    """Return a deterministic escalation decision without invoking a model.

    ``search_result`` is the canonical reuse-search evidence. When it is supplied, BUILD is
    allowed only after at least one canonical source was attempted and no compatible capability
    was found. A discovered compatible candidate wins over a caller's stale/mistaken
    ``MISSING_CAPABILITY`` label. Conversely, a caller claiming an existing capability while the
    canonical search cannot currently supply it is blocked as unavailable rather than rebuilt.

    The optional legacy path is retained for current orchestration callers until the Toolsmith
    service owner wires the canonical ``ReuseSearchResult`` through its owned dispatch seam.
    """

    if gap.kind in _BLOCKING_KINDS:
        return GapDecision(GapDisposition.BLOCK, f"gap kind {gap.kind.value} cannot trigger build")

    if search_result is not None:
        if not search_result.attempted_sources:
            return GapDecision(GapDisposition.BLOCK, "canonical reuse search attempted no sources")
        if any(
            candidate.capability_id != gap.requested_capability
            for candidate in search_result.candidates
        ):
            return GapDecision(GapDisposition.BLOCK, "canonical reuse search evidence capability mismatch")

        compatible = tuple(
            candidate
            for candidate in search_result.candidates
            if candidate.permissions.issubset(gap.permission_ceiling)
        )
        if compatible:
            return GapDecision(GapDisposition.REUSE, "canonical reuse search found compatible capability")
        if search_result.candidates:
            return GapDecision(
                GapDisposition.BLOCK,
                "canonical reuse search found capability but task permission ceiling rejects it",
            )
        if gap.kind is GapKind.EXISTING_CAPABILITY_AVAILABLE:
            return GapDecision(
                GapDisposition.BLOCK,
                "existing capability is currently unavailable in canonical reuse search",
            )
        if gap.kind is not GapKind.MISSING_CAPABILITY:
            return GapDecision(GapDisposition.BLOCK, "unsupported gap kind")
        if not gap.permission_ceiling:
            return GapDecision(GapDisposition.BLOCK, "missing task permission ceiling")
        return GapDecision(
            GapDisposition.BUILD,
            "capability is genuinely missing after canonical deterministic reuse search",
        )

    # Compatibility path for the current CapabilityEscalationService.begin() owner. The
    # production integration must pass ReuseSearchResult before relying on BUILD eligibility.
    if gap.kind is GapKind.EXISTING_CAPABILITY_AVAILABLE:
        return GapDecision(GapDisposition.REUSE, "existing capability is available")
    if gap.kind is not GapKind.MISSING_CAPABILITY:
        return GapDecision(GapDisposition.BLOCK, "unsupported gap kind")
    if not gap.attempted_methods:
        return GapDecision(GapDisposition.BLOCK, "missing capability search evidence")
    if not gap.permission_ceiling:
        return GapDecision(GapDisposition.BLOCK, "missing task permission ceiling")
    return GapDecision(GapDisposition.BUILD, "capability is genuinely missing after deterministic search")
