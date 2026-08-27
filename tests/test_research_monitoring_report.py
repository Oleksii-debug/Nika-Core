from __future__ import annotations

import pytest

from nika_core.research.models import (
    FreshnessState,
    RefreshDisposition,
    ResearchEvidence,
    ResearchResultItem,
    SourceKind,
)
from nika_core.research.monitoring_report import (
    MonitoringCheck,
    MonitoringReport,
    MonitoringSourceCheck,
    changes_from_profile_delta,
    render_monitoring_report_text,
)
from nika_core.research.scheduled_profiles import (
    ResearchDeltaItem,
    ResearchDeltaKind,
    ResearchProfileDelta,
)


def _source(
    *,
    disposition: RefreshDisposition = RefreshDisposition.UNCHANGED,
    attempts: int = 1,
    error_code: str | None = None,
) -> MonitoringSourceCheck:
    return MonitoringSourceCheck(
        source_id="source-news",
        source_kind=SourceKind.HTTP,
        disposition=disposition,
        attempts=attempts,
        error_code=error_code,
        snapshot_id="snapshot-1" if disposition is RefreshDisposition.CHANGED else None,
    )


def _check(
    checked_at: str,
    *,
    condition_matched: bool = False,
    sources: tuple[MonitoringSourceCheck, ...] | None = None,
) -> MonitoringCheck:
    return MonitoringCheck(
        check_id=f"check-{checked_at}",
        checked_at=checked_at,
        sources=sources or (_source(),),
        changes=(),
        condition_matched=condition_matched,
    )


def test_monitoring_report_renders_required_accessible_timeline_fields() -> None:
    report = MonitoringReport(
        monitor_id="monitor-1",
        checks=(
            _check(
                "2026-08-27T15:00:00+00:00",
                sources=(
                    _source(
                        disposition=RefreshDisposition.FAILED,
                        attempts=3,
                        error_code="timeout",
                    ),
                ),
            ),
            _check("2026-08-27T15:05:00+00:00"),
        ),
        next_scheduled_check="2026-08-27T15:10:00+00:00",
        state_reference="checkpoint-7",
    )

    text = render_monitoring_report_text(report)

    assert "Monitoring report" in text
    assert "source-news [http]: check failed; retries=2; error=timeout" in text
    assert "Check time: 2026-08-27T15:05:00+00:00" in text
    assert "Condition: not matched" in text
    assert "What changed: no normalized result change recorded" in text
    assert "Next scheduled check: 2026-08-27T15:10:00+00:00" in text
    assert "Terminal reason: none" in text
    assert "State reference: checkpoint-7" in text


def test_profile_delta_projection_keeps_provenance_but_not_raw_locator_or_snippet() -> None:
    secret_locator = "https://example.test/item?access_token=SUPER-SECRET"
    item = ResearchResultItem(
        ordinal=0,
        document_id="doc-1",
        title="New <strong>headline</strong> token=SHOULD-REDACT",
        snippet="<html>RAW BODY THAT MUST NOT BE EXPORTED</html>",
        rank=1.0,
        why_matched="literal match",
        evidence=(
            ResearchEvidence(
                source_id="source-news",
                source_kind=SourceKind.HTTP,
                locator=secret_locator,
                observed_at="2026-08-27T15:00:00+00:00",
                freshness=FreshnessState.CURRENT,
            ),
        ),
    )
    delta = ResearchProfileDelta(
        task_id="task-1",
        series_id="monitor-1",
        result_set_id="result-2",
        previous_result_set_id="result-1",
        items=(ResearchDeltaItem(0, ResearchDeltaKind.NEW, item),),
    )
    changes = changes_from_profile_delta(delta)
    report = MonitoringReport(
        monitor_id="monitor-1",
        checks=(
            MonitoringCheck(
                check_id="task-1",
                checked_at="2026-08-27T15:00:00+00:00",
                sources=(_source(disposition=RefreshDisposition.CHANGED),),
                changes=changes,
                condition_matched=True,
                result_set_id="result-2",
                previous_result_set_id="result-1",
            ),
        ),
        terminal_reason="condition_matched",
    )

    text = render_monitoring_report_text(report)

    assert "New ‹strong›headline‹/strong› token=[redacted]" in text
    assert "Provenance: http source=source-news" in text
    assert "observed=2026-08-27T15:00:00+00:00" in text
    assert "result-1" in text
    assert "result-2" in text
    assert "SUPER-SECRET" not in text
    assert "RAW BODY" not in text
    assert "https://example.test" not in text


def test_matched_condition_cannot_report_future_schedule() -> None:
    with pytest.raises(ValueError, match="matched condition"):
        MonitoringReport(
            monitor_id="monitor-1",
            checks=(
                _check(
                    "2026-08-27T15:00:00+00:00",
                    condition_matched=True,
                ),
            ),
            next_scheduled_check="2026-08-27T15:05:00+00:00",
        )


def test_terminal_report_cannot_report_future_schedule() -> None:
    with pytest.raises(ValueError, match="terminal monitoring report"):
        MonitoringReport(
            monitor_id="monitor-1",
            checks=(_check("2026-08-27T15:00:00+00:00"),),
            next_scheduled_check="2026-08-27T15:05:00+00:00",
            terminal_reason="deadline_reached",
        )


def test_checks_must_be_chronological() -> None:
    with pytest.raises(ValueError, match="chronologically"):
        MonitoringReport(
            monitor_id="monitor-1",
            checks=(
                _check("2026-08-27T15:05:00+00:00"),
                _check("2026-08-27T15:00:00+00:00"),
            ),
        )


def test_timestamps_must_be_timezone_aware() -> None:
    with pytest.raises(ValueError, match="timezone offset"):
        _check("2026-08-27T15:00:00")


def test_renderer_bounds_spoken_history_but_reports_omitted_count() -> None:
    checks = tuple(
        _check(f"2026-08-27T15:{minute:02d}:00+00:00") for minute in range(6)
    )
    text = render_monitoring_report_text(
        MonitoringReport(monitor_id="monitor-1", checks=checks),
        max_checks=2,
    )

    assert "Checks recorded: 6" in text
    assert "History: showing latest 2; 4 earlier checks omitted" in text
    assert "Check 5" in text
    assert "Check 6" in text
    assert "Check 1" not in text
