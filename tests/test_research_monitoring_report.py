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
    MonitoringChange,
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
    source_id: str = "source-news",
    disposition: RefreshDisposition = RefreshDisposition.UNCHANGED,
    attempts: int = 1,
    error_code: str | None = None,
) -> MonitoringSourceCheck:
    return MonitoringSourceCheck(
        source_id=source_id,
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
    next_scheduled_check: str | None = None,
    terminal_reason: str | None = None,
) -> MonitoringCheck:
    return MonitoringCheck(
        check_id=f"check-{checked_at}",
        checked_at=checked_at,
        sources=sources or (_source(),),
        changes=(),
        condition_matched=condition_matched,
        next_scheduled_check=next_scheduled_check,
        terminal_reason=terminal_reason,
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
            _check(
                "2026-08-27T15:05:00+00:00",
                next_scheduled_check="2026-08-27T15:10:00+00:00",
            ),
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
                terminal_reason="condition_matched",
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
                    next_scheduled_check="2026-08-27T15:05:00+00:00",
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


def test_per_check_schedule_terminal_snapshots_render_in_stable_order() -> None:
    check = MonitoringCheck(
        check_id="check-1",
        checked_at="2026-08-27T15:00:00+00:00",
        sources=(_source(),),
        changes=(),
        condition_matched=False,
        next_scheduled_check="2026-08-27T16:00:00+00:00",
    )
    report = MonitoringReport(
        monitor_id="monitor-1",
        checks=(check,),
        next_scheduled_check="2026-08-27T16:00:00+00:00",
    )

    rendered = render_monitoring_report_text(report)
    check_block = rendered[rendered.index("Check 1") :]
    fields = (
        "Check reference:",
        "Check time:",
        "Condition:",
        "Next scheduled check:",
        "Terminal reason:",
        "Sources:",
        "What changed:",
        "Evidence summary:",
    )
    offsets = tuple(check_block.index(field) for field in fields)
    assert offsets == tuple(sorted(offsets))
    assert "Evidence summary: 0 normalized change reference(s); 0 provenance reference(s); 1 source outcome(s)" in rendered


def test_report_rejects_global_state_that_contradicts_latest_check_snapshot() -> None:
    check = MonitoringCheck(
        check_id="check-1",
        checked_at="2026-08-27T15:00:00+00:00",
        sources=(_source(),),
        changes=(),
        condition_matched=False,
        next_scheduled_check="2026-08-27T16:00:00+00:00",
    )
    with pytest.raises(ValueError, match="contradicts latest check snapshot"):
        MonitoringReport(
            monitor_id="monitor-1",
            checks=(check,),
            next_scheduled_check="2026-08-27T17:00:00+00:00",
        )


@pytest.mark.parametrize(
    "unsafe",
    (
        "access_token=CANARY",
        "Authorization:Bearer",
        "cookie=session",
        "two words",
        "code?query",
    ),
)
def test_error_and_terminal_codes_reject_free_form_or_assignment_material(unsafe: str) -> None:
    with pytest.raises(ValueError, match="bounded safe code"):
        MonitoringSourceCheck(
            source_id="source-news",
            source_kind=SourceKind.HTTP,
            disposition=RefreshDisposition.FAILED,
            attempts=1,
            error_code=unsafe,
        )
    with pytest.raises(ValueError, match="bounded safe code"):
        MonitoringReport(monitor_id="monitor-1", checks=(), terminal_reason=unsafe)


def test_authorization_and_cookie_canaries_are_fully_redacted_from_change_titles() -> None:
    change = MonitoringChange(
        kind="changed",
        document_id="doc-1",
        title="Authorization: Bearer CANARY Cookie: session=COOKIECANARY",
    )
    check = MonitoringCheck(
        check_id="check-1",
        checked_at="2026-08-27T15:00:00+00:00",
        sources=(_source(),),
        changes=(change,),
        condition_matched=False,
    )
    rendered = render_monitoring_report_text(MonitoringReport("monitor-1", (check,)))
    assert "CANARY" not in rendered
    assert "COOKIECANARY" not in rendered
    assert "[redacted]" in rendered


def test_source_rows_are_canonicalized_and_duplicate_identity_is_rejected() -> None:
    second = MonitoringSourceCheck(
        source_id="a-source",
        source_kind=SourceKind.HTTP,
        disposition=RefreshDisposition.UNCHANGED,
        attempts=1,
    )
    first = MonitoringSourceCheck(
        source_id="z-source",
        source_kind=SourceKind.HTTP,
        disposition=RefreshDisposition.UNCHANGED,
        attempts=1,
    )
    check = MonitoringCheck(
        check_id="check-1",
        checked_at="2026-08-27T15:00:00+00:00",
        sources=(first, second),
        changes=(),
        condition_matched=False,
    )
    assert tuple(item.source_id for item in check.sources) == ("a-source", "z-source")

    with pytest.raises(ValueError, match="duplicate stable identities"):
        MonitoringCheck(
            check_id="check-dup",
            checked_at="2026-08-27T15:00:00+00:00",
            sources=(second, second),
            changes=(),
            condition_matched=False,
        )


def test_terminal_check_snapshot_cannot_advertise_future_schedule() -> None:
    with pytest.raises(ValueError, match="terminal monitoring check"):
        MonitoringCheck(
            check_id="check-1",
            checked_at="2026-08-27T15:00:00+00:00",
            sources=(_source(),),
            changes=(),
            condition_matched=False,
            next_scheduled_check="2026-08-27T16:00:00+00:00",
            terminal_reason="deadline_reached",
        )


@pytest.mark.parametrize(
    "unsafe",
    (
        "token=REPORT_CANARY",
        "Authorization:BearerCANARY",
        "cookie:SESSIONCANARY",
        "https://example.test/path",
        "two words",
    ),
)
def test_rendered_machine_references_reject_secret_or_url_shapes(unsafe: str) -> None:
    with pytest.raises(ValueError, match="safe reference"):
        MonitoringSourceCheck(
            source_id=unsafe,
            source_kind=SourceKind.HTTP,
            disposition=RefreshDisposition.UNCHANGED,
            attempts=1,
        )
    with pytest.raises(ValueError, match="safe reference"):
        MonitoringSourceCheck(
            source_id="source-news",
            source_kind=SourceKind.HTTP,
            disposition=RefreshDisposition.CHANGED,
            attempts=1,
            snapshot_id=unsafe,
        )
    with pytest.raises(ValueError, match="safe reference"):
        MonitoringChange(kind="changed", document_id=unsafe, title="safe title")
    with pytest.raises(ValueError, match="safe reference"):
        MonitoringCheck(
            check_id=unsafe,
            checked_at="2026-08-27T15:00:00+00:00",
            sources=(_source(),),
            changes=(),
            condition_matched=False,
        )
    with pytest.raises(ValueError, match="safe reference"):
        MonitoringCheck(
            check_id="check-1",
            checked_at="2026-08-27T15:00:00+00:00",
            sources=(_source(),),
            changes=(),
            condition_matched=False,
            result_set_id=unsafe,
        )
    with pytest.raises(ValueError, match="safe reference"):
        MonitoringReport(monitor_id=unsafe, checks=())
    with pytest.raises(ValueError, match="safe reference"):
        MonitoringReport(
            monitor_id="monitor-1",
            checks=(),
            state_reference=unsafe,
        )


def test_change_provenance_must_belong_to_check_source_identity() -> None:
    foreign_source = MonitoringChange(
        kind="changed",
        document_id="doc-1",
        title="foreign source",
        evidence=(
            ResearchEvidence(
                source_id="source-other",
                source_kind=SourceKind.HTTP,
                locator="https://example.test/other",
                observed_at="2026-08-27T15:00:00+00:00",
                freshness=FreshnessState.CURRENT,
            ),
        ),
    )
    foreign_kind = MonitoringChange(
        kind="changed",
        document_id="doc-2",
        title="foreign kind",
        evidence=(
            ResearchEvidence(
                source_id="source-news",
                source_kind=SourceKind.LOCAL_FILE,
                locator="C:/Corpus/source-news.txt",
                observed_at="2026-08-27T15:00:00+00:00",
                freshness=FreshnessState.CURRENT,
            ),
        ),
    )

    for change in (foreign_source, foreign_kind):
        with pytest.raises(ValueError, match="not part of this monitoring check"):
            MonitoringCheck(
                check_id="check-1",
                checked_at="2026-08-27T15:00:00+00:00",
                sources=(_source(),),
                changes=(change,),
                condition_matched=False,
            )


def test_evidence_reference_and_timestamp_are_safe_before_rendering() -> None:
    with pytest.raises(ValueError, match="safe reference"):
        MonitoringChange(
            kind="changed",
            document_id="doc-1",
            title="safe",
            evidence=(
                ResearchEvidence(
                    source_id="token=EVIDENCE_CANARY",
                    source_kind=SourceKind.HTTP,
                    locator="https://example.test/safe",
                    observed_at="2026-08-27T15:00:00+00:00",
                    freshness=FreshnessState.CURRENT,
                ),
            ),
        )

    with pytest.raises(ValueError):
        MonitoringChange(
            kind="changed",
            document_id="doc-1",
            title="safe",
            evidence=(
                ResearchEvidence(
                    source_id="source-news",
                    source_kind=SourceKind.HTTP,
                    locator="https://example.test/safe",
                    observed_at="2026-08-27T15:00:00",
                    freshness=FreshnessState.CURRENT,
                ),
            ),
        )


def test_quoted_secret_assignments_are_fully_redacted_from_titles() -> None:
    change = MonitoringChange(
        kind="changed",
        document_id="doc-1",
        title='token="SECRET PART TWO" client_secret=\'SECOND SECRET VALUE\'',
    )
    check = MonitoringCheck(
        check_id="check-1",
        checked_at="2026-08-27T15:00:00+00:00",
        sources=(_source(),),
        changes=(change,),
        condition_matched=False,
    )
    rendered = render_monitoring_report_text(MonitoringReport("monitor-1", (check,)))

    assert "SECRET PART TWO" not in rendered
    assert "SECOND SECRET VALUE" not in rendered
    assert rendered.count("[redacted]") >= 2
