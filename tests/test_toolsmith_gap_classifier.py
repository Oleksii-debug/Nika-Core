from __future__ import annotations

import pytest

from nika_core.toolsmith import (
    CapabilityGap,
    GapDisposition,
    GapKind,
    ReuseCandidate,
    ReuseSearchResult,
    classify_gap,
)


def _gap(kind: GapKind) -> CapabilityGap:
    return CapabilityGap(
        task_id="task-gap-classifier",
        requested_capability="tool.example",
        kind=kind,
        reason="deterministic classifier fixture",
        attempted_methods=("legacy-non-authoritative-marker",),
        permission_ceiling=frozenset({"fs.read"}),
    )


def _search(*candidates: ReuseCandidate) -> ReuseSearchResult:
    return ReuseSearchResult(
        candidates=tuple(candidates),
        attempted_sources=("tool_registry", "plugin_registry"),
    )


def _candidate(*, permissions: frozenset[str] = frozenset({"fs.read"})) -> ReuseCandidate:
    return ReuseCandidate(
        capability_id="tool.example",
        version="1.0.0",
        source="tool_registry",
        digest="sha256:classifier-fixture",
        permissions=permissions,
    )


def test_truly_missing_capability_builds_only_after_canonical_search_miss() -> None:
    decision = classify_gap(_gap(GapKind.MISSING_CAPABILITY), search_result=_search())
    assert decision.disposition is GapDisposition.BUILD
    assert "canonical" in decision.reason


def test_existing_capability_unavailable_blocks_instead_of_building() -> None:
    decision = classify_gap(
        _gap(GapKind.EXISTING_CAPABILITY_AVAILABLE),
        search_result=_search(),
    )
    assert decision.disposition is GapDisposition.BLOCK
    assert "unavailable" in decision.reason


def test_permission_denied_never_builds() -> None:
    decision = classify_gap(
        _gap(GapKind.PERMISSION_DENIED),
        search_result=_search(),
    )
    assert decision.disposition is GapDisposition.BLOCK


@pytest.mark.parametrize("kind", [GapKind.MISSING_INFORMATION, GapKind.AMBIGUOUS_GOAL])
def test_bad_input_classes_never_build(kind: GapKind) -> None:
    decision = classify_gap(_gap(kind), search_result=_search())
    assert decision.disposition is GapDisposition.BLOCK


@pytest.mark.parametrize("kind", [GapKind.TOOL_FAILED, GapKind.MODEL_FAILED])
def test_provider_failure_classes_never_build(kind: GapKind) -> None:
    decision = classify_gap(_gap(kind), search_result=_search())
    assert decision.disposition is GapDisposition.BLOCK


def test_search_evidence_found_overrides_stale_missing_label_to_reuse() -> None:
    decision = classify_gap(
        _gap(GapKind.MISSING_CAPABILITY),
        search_result=_search(_candidate()),
    )
    assert decision.disposition is GapDisposition.REUSE


def test_search_evidence_found_but_permission_incompatible_blocks_build() -> None:
    decision = classify_gap(
        _gap(GapKind.MISSING_CAPABILITY),
        search_result=_search(_candidate(permissions=frozenset({"network.any"}))),
    )
    assert decision.disposition is GapDisposition.BLOCK
    assert "permission ceiling" in decision.reason


def test_empty_search_provenance_cannot_authorize_build() -> None:
    decision = classify_gap(
        _gap(GapKind.MISSING_CAPABILITY),
        search_result=ReuseSearchResult(candidates=(), attempted_sources=()),
    )
    assert decision.disposition is GapDisposition.BLOCK


def test_mismatched_search_evidence_cannot_authorize_build() -> None:
    foreign = ReuseCandidate(
        capability_id="tool.other",
        version="1.0.0",
        source="tool_registry",
        digest="sha256:foreign",
        permissions=frozenset({"fs.read"}),
    )
    decision = classify_gap(
        _gap(GapKind.MISSING_CAPABILITY),
        search_result=_search(foreign),
    )
    assert decision.disposition is GapDisposition.BLOCK
