from __future__ import annotations

from dataclasses import dataclass

import pytest

from nika_core.research.models import (
    FreshnessState,
    HttpSourceState,
    ResearchResultItem,
    ResearchResultSet,
)
from nika_core.research.monitoring import (
    SourceHealthStatus,
    build_workspace_health_report,
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
