from __future__ import annotations

from dataclasses import dataclass

import pytest

from nika_core.research.models import (
    FreshnessState,
    HttpSourceState,
    ResearchEvidence,
    ResearchResultItem,
    ResearchResultSet,
    SourceKind,
)
from nika_core.research.monitoring import (
    ChangeDetectionStatus,
    NormalizedObservation,
    SourceHealthStatus,
    build_workspace_health_report,
    detect_observation_change,
    diff_result_sets,
)


def _source(source_id: str, freshness: FreshnessState) -> HttpSourceState:
    return HttpSourceState(
        source_id=source_id,
        workspace_id="ws",
        url=f"https://example.com/{source_id}",
        final_url=f"https://example.com/{source_id}",
        etag=None,
        last_modified=None,
        current_raw_sha256="abc" if freshness is not FreshnessState.UNKNOWN else None,
        freshness=freshness,
        last_attempt_at="2026-08-20T00:00:00+00:00",
        last_success_at=(
            "2026-08-19T23:00:00+00:00"
            if freshness in {FreshnessState.CURRENT, FreshnessState.STALE}
            else None
        ),
        last_status_code=200 if freshness is FreshnessState.CURRENT else None,
        last_error_code="fetch_failed" if freshness is FreshnessState.ERROR else None,
        last_error_message="network failure" if freshness is FreshnessState.ERROR else None,
    )


@dataclass
class _Repository:
    sources: tuple[HttpSourceState, ...]

    def list_sources(self, workspace_id: str) -> tuple[HttpSourceState, ...]:
        assert workspace_id == "ws"
        return tuple(sorted(self.sources, key=lambda source: source.source_id))


def _item(document_id: str, rank: float) -> ResearchResultItem:
    return ResearchResultItem(
        ordinal=0,
        document_id=document_id,
        title=document_id,
        snippet=document_id,
        rank=rank,
        why_matched="literal",
        evidence=(),
    )


def _result(result_set_id: str, items: tuple[ResearchResultItem, ...]) -> ResearchResultSet:
    return ResearchResultSet(
        result_set_id=result_set_id,
        workspace_id="ws",
        query="grant",
        items=items,
        created_at="2026-08-20T00:00:00+00:00",
    )


def _evidence(
    *,
    source_id: str = "web-a",
    locator: str = "https://example.com/a",
    observed_at: str = "2026-08-20T00:00:00+00:00",
) -> ResearchEvidence:
    return ResearchEvidence(
        source_id=source_id,
        source_kind=SourceKind.HTTP,
        locator=locator,
        observed_at=observed_at,
        freshness=FreshnessState.CURRENT,
    )


def _observation(
    content_id: str | None,
    *,
    evidence: tuple[ResearchEvidence, ...] | None = None,
    condition_matched: bool = False,
    error_code: str | None = None,
    error_message: str | None = None,
) -> NormalizedObservation:
    return NormalizedObservation(
        source_id="web-a",
        source_kind=SourceKind.HTTP,
        content_id=content_id,
        evidence=(_evidence(),) if evidence is None else evidence,
        condition_matched=condition_matched,
        error_code=error_code,
        error_message=error_message,
    )


def test_workspace_health_report_is_deterministic_and_complete() -> None:
    repository = _Repository(
        (
            _source("stale", FreshnessState.STALE),
            _source("healthy", FreshnessState.CURRENT),
            _source("unknown", FreshnessState.UNKNOWN),
            _source("removed", FreshnessState.REMOVED),
            _source("blocked", FreshnessState.BLOCKED),
            _source("error", FreshnessState.ERROR),
        )
    )

    report = build_workspace_health_report(repository, "ws")  # type: ignore[arg-type]

    assert tuple(source.source_id for source in report.sources) == (
        "blocked",
        "error",
        "healthy",
        "removed",
        "stale",
        "unknown",
    )
    assert report.healthy == 1
    assert report.degraded == 2
    assert report.blocked == 1
    assert report.removed == 1
    assert report.unknown == 1
    assert report.sources[0].status is SourceHealthStatus.BLOCKED
    assert report.sources[1].last_error_code == "fetch_failed"


def test_observation_detector_ignores_timestamp_and_locator_noise() -> None:
    previous_evidence = (
        _evidence(
            locator="https://example.com/old-final",
            observed_at="2026-08-20T00:00:00+00:00",
        ),
    )
    current_evidence = (
        _evidence(
            locator="https://example.com/new-final",
            observed_at="2026-08-21T00:00:00+00:00",
        ),
    )

    result = detect_observation_change(
        _observation("normalized-doc-a", evidence=previous_evidence),
        _observation("normalized-doc-a", evidence=current_evidence),
    )

    assert result.status is ChangeDetectionStatus.UNCHANGED
    assert result.changed is False
    assert result.previous_evidence == previous_evidence
    assert result.current_evidence == current_evidence


def test_observation_detector_reports_changed_normalized_content() -> None:
    result = detect_observation_change(
        _observation("normalized-doc-a"),
        _observation("normalized-doc-b"),
    )

    assert result.status is ChangeDetectionStatus.CHANGED
    assert result.changed is True
    assert result.source_id == "web-a"
    assert result.source_kind is SourceKind.HTTP
    assert result.previous_content_id == "normalized-doc-a"
    assert result.current_content_id == "normalized-doc-b"


def test_observation_detector_reports_condition_match_without_fabricating_change() -> None:
    result = detect_observation_change(
        _observation("normalized-doc-a"),
        _observation("normalized-doc-a", condition_matched=True),
    )

    assert result.status is ChangeDetectionStatus.CONDITION_MATCHED
    assert result.condition_matched is True
    assert result.changed is False


def test_observation_detector_reports_error_and_unknown_explicitly() -> None:
    error = detect_observation_change(
        _observation("normalized-doc-a"),
        _observation(None, error_code="fetch_failed", error_message="fixture failure"),
    )
    unknown = detect_observation_change(None, _observation("normalized-doc-a"))

    assert error.status is ChangeDetectionStatus.ERROR
    assert error.changed is None
    assert error.error_code == "fetch_failed"
    assert error.error_message == "fixture failure"
    assert unknown.status is ChangeDetectionStatus.UNKNOWN
    assert unknown.changed is None


def test_observation_detector_rejects_cross_source_comparison() -> None:
    current = NormalizedObservation(
        source_id="web-b",
        source_kind=SourceKind.HTTP,
        content_id="normalized-doc-b",
        evidence=(_evidence(source_id="web-b"),),
    )

    with pytest.raises(ValueError, match="same stable source"):
        detect_observation_change(_observation("normalized-doc-a"), current)


def test_observation_detector_rejects_malformed_snapshot_evidence() -> None:
    malformed = _observation(
        "normalized-doc-a",
        evidence=(_evidence(source_id="wrong-source"),),
    )

    with pytest.raises(ValueError, match="evidence source_id mismatch"):
        detect_observation_change(_observation("normalized-doc-a"), malformed)


def test_result_delta_tracks_exact_identity_and_rank_changes() -> None:
    previous = _result("old", (_item("a", 1.0), _item("b", 2.0), _item("d", 4.0)))
    current = _result("new", (_item("b", 1.5), _item("c", 3.0), _item("d", 4.0)))

    delta = diff_result_sets(previous, current)

    assert delta.changed is True
    assert delta.added_document_ids == ("c",)
    assert delta.removed_document_ids == ("a",)
    assert delta.retained_document_ids == ("b", "d")
    assert delta.rank_changed_document_ids == ("b",)


def test_result_delta_is_unchanged_when_only_result_set_identity_changes() -> None:
    previous = _result("old", (_item("a", 1.0),))
    current = _result("new", (_item("a", 1.0),))

    delta = diff_result_sets(previous, current)

    assert delta.changed is False
    assert delta.added_document_ids == ()
    assert delta.removed_document_ids == ()
    assert delta.rank_changed_document_ids == ()


def test_result_delta_rejects_cross_workspace_comparison() -> None:
    previous = _result("old", (_item("a", 1.0),))
    current = ResearchResultSet(
        result_set_id="new",
        workspace_id="other",
        query="grant",
        items=(_item("a", 1.0),),
        created_at="2026-08-20T00:00:00+00:00",
    )

    with pytest.raises(ValueError, match="same workspace"):
        diff_result_sets(previous, current)
