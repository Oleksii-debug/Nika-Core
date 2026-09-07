from __future__ import annotations

import csv
import hashlib
from io import BytesIO, StringIO
from zipfile import ZipFile

import pytest
from docx import Document
from openpyxl import load_workbook

from nika_core.research.models import FreshnessState, ResearchEvidence, SourceKind
from nika_core.research.report_exports import ResearchReportExporter, ResearchReportFormat
from nika_core.research.review import (
    AccessibleResearchReport,
    ResearchCard,
    ResearchReview,
    ResearchReviewState,
    render_accessible_report_text,
)


def _report() -> AccessibleResearchReport:
    card = ResearchCard(
        ordinal=0,
        document_id="doc-1",
        title="=1+1 Українська <можливість>",
        snippet="Грант & навчання <script>alert(1)</script>",
        rank=-1.25,
        why_matched="Literal match: грант",
        evidence=(
            ResearchEvidence(
                source_id="source-1",
                source_kind=SourceKind.HTTP,
                locator="https://example.org/?a=1&b=2",
                observed_at="2026-08-20T07:00:00+00:00",
                freshness=FreshnessState.CURRENT,
            ),
        ),
        review=ResearchReview(
            workspace_id="ws",
            document_id="doc-1",
            state=ResearchReviewState.SAVED,
            note="  @SUM(A1:A2) перевірити вручну",
            updated_at="2026-08-20T07:10:00+00:00",
        ),
    )
    return AccessibleResearchReport(
        result_set_id="../results/unsafe id",
        workspace_id="ws",
        query="грант & навчання",
        created_at="2026-08-20T07:00:00+00:00",
        cards=(card,),
        text=(
            "Research results\n"
            "Query: грант & навчання\n"
            "Results: 1\n"
            "Result 1: =1+1 Українська <можливість>\n"
        ),
    )


def test_txt_export_is_utf8_and_returns_safe_leaf_filename() -> None:
    rendered = ResearchReportExporter().render(_report(), ResearchReportFormat.TXT)

    assert rendered.filename == "research-results-results-unsafe-id.txt"
    assert "/" not in rendered.filename
    assert "\\" not in rendered.filename
    assert rendered.media_type == "text/plain; charset=utf-8"
    assert rendered.content.decode() == render_accessible_report_text(_report())
    assert rendered.sha256 == hashlib.sha256(rendered.content).hexdigest()


def test_csv_preserves_review_provenance_and_blocks_formula_injection() -> None:
    rendered = ResearchReportExporter().render(_report(), ResearchReportFormat.CSV)
    rows = list(csv.DictReader(StringIO(rendered.content.decode())))

    assert len(rows) == 1
    row = rows[0]
    assert row["title"] == "'=1+1 Українська <можливість>"
    assert row["review_state"] == "saved"
    assert row["review_note"] == "'  @SUM(A1:A2) перевірити вручну"
    assert row["review_updated_at"] == "2026-08-20T07:10:00+00:00"
    assert row["source_id"] == "source-1"
    assert row["source_kind"] == "http"
    assert row["freshness"] == "current"
    assert row["locator"] == "http-source"
    assert row["observed_at"] == "2026-08-20T07:00:00+00:00"


def test_html_is_semantic_and_escapes_untrusted_text() -> None:
    rendered = ResearchReportExporter().render(
        _report(),
        ResearchReportFormat.HTML,
        language_tag="uk",
    )
    text = rendered.content.decode()

    assert '<html lang="uk">' in text
    assert '<h1 lang="en">Research results</h1>' in text
    assert "<main>" in text
    assert '<article aria-labelledby="result-1">' in text
    assert (
        '<h2 id="result-1"><span lang="en">Result 1: </span>'
        '=1+1 Українська &lt;можливість&gt;</h2>'
        in text
    )
    assert "Грант &amp; навчання &lt;script&gt;alert(1)&lt;/script&gt;" in text
    assert '<dt lang="en">Review updated</dt><dd>2026-08-20T07:10:00+00:00</dd>' in text
    assert "<dt lang=\"en\">Location</dt><dd>http-source</dd>" in text
    assert "https://example.org/" not in text
    assert "<script>alert(1)</script>" not in text


def test_docx_has_heading_hierarchy_labels_review_and_provenance() -> None:
    rendered = ResearchReportExporter().render(_report(), ResearchReportFormat.DOCX)
    document = Document(BytesIO(rendered.content))
    paragraphs = [(paragraph.style.name, paragraph.text) for paragraph in document.paragraphs]

    assert ("Heading 1", "Research results") in paragraphs
    assert ("Heading 2", "Result 1: =1+1 Українська <можливість>") in paragraphs
    assert ("Heading 3", "Evidence") in paragraphs
    assert ("Normal", "Review: saved") in paragraphs
    assert ("Normal", "Review updated: 2026-08-20T07:10:00+00:00") in paragraphs
    assert ("Normal", "Source ID: source-1") in paragraphs
    assert ("Normal", "Location: http-source") in paragraphs
    assert document.core_properties.title == "Research results"
    assert document.core_properties.author == "Nika Core"
    assert document.core_properties.created.isoformat() == "2026-08-20T07:00:00+00:00"
    assert document.core_properties.modified.isoformat() == "2026-08-20T07:00:00+00:00"


def test_xlsx_is_flat_accessible_and_blocks_formula_injection() -> None:
    rendered = ResearchReportExporter().render(_report(), ResearchReportFormat.XLSX)
    workbook = load_workbook(BytesIO(rendered.content), data_only=False)

    assert workbook.sheetnames == ["Metadata", "Results"]
    assert workbook["Metadata"]["A1"].value == "Field"
    assert workbook["Metadata"].freeze_panes == "A2"
    assert workbook["Results"].freeze_panes == "A2"
    assert not workbook["Metadata"].merged_cells.ranges
    assert not workbook["Results"].merged_cells.ranges
    headers = [cell.value for cell in workbook["Results"][1]]
    row = [cell.value for cell in workbook["Results"][2]]
    values = dict(zip(headers, row, strict=True))
    assert values["title"] == "'=1+1 Українська <можливість>"
    assert values["review_note"] == "'  @SUM(A1:A2) перевірити вручну"
    assert values["review_updated_at"] == "2026-08-20T07:10:00+00:00"
    assert values["source_id"] == "source-1"
    assert values["locator"] == "http-source"
    assert workbook["Results"]["G2"].data_type == "s"
    assert workbook.properties.title == "Research results"
    assert workbook.properties.creator == "Nika Core"
    assert workbook.properties.created.isoformat() == "2026-08-20T07:00:00"
    assert workbook.properties.modified.isoformat() == "2026-08-20T07:00:00"


def test_all_formats_redact_raw_http_and_local_locator_secrets() -> None:
    report = _report()
    card = report.cards[0]
    hostile_http = ResearchEvidence(
        source_id="http-source-id",
        source_kind=SourceKind.HTTP,
        locator=(
            "https://resolver-user:resolver-pass@example.org/private/HTTP_PATH_CANARY"
            "?access_token=HTTP_QUERY_CANARY#HTTP_FRAGMENT_CANARY"
        ),
        observed_at="2026-08-20T07:00:00+00:00",
        freshness=FreshnessState.CURRENT,
    )
    hostile_local = ResearchEvidence(
        source_id="local-source-id",
        source_kind=SourceKind.LOCAL_FILE,
        locator=r"C:\Users\Private User\Secrets\LOCAL_PATH_CANARY.txt",
        observed_at="2026-08-20T07:01:00+00:00",
        freshness=FreshnessState.CURRENT,
    )
    hostile_card = ResearchCard(
        ordinal=card.ordinal,
        document_id=card.document_id,
        title=card.title,
        snippet=card.snippet,
        rank=card.rank,
        why_matched=card.why_matched,
        evidence=(hostile_http, hostile_local),
        review=card.review,
    )
    hostile_report = AccessibleResearchReport(
        result_set_id=report.result_set_id,
        workspace_id=report.workspace_id,
        query=report.query,
        created_at=report.created_at,
        cards=(hostile_card,),
        text=(
            report.text
            + "\nLocation: https://resolver-user:resolver-pass@example.org/"
            + "private/HTTP_PATH_CANARY?access_token=HTTP_QUERY_CANARY"
            + "#HTTP_FRAGMENT_CANARY\n"
            + r"Location: C:\Users\Private User\Secrets\LOCAL_PATH_CANARY.txt"
            + "\n"
        ),
    )

    canaries = (
        "resolver-user",
        "resolver-pass",
        "HTTP_PATH_CANARY",
        "HTTP_QUERY_CANARY",
        "HTTP_FRAGMENT_CANARY",
        "Private User",
        "LOCAL_PATH_CANARY",
    )
    exporter = ResearchReportExporter()
    for report_format in ResearchReportFormat:
        kwargs = {"language_tag": "uk"} if report_format is ResearchReportFormat.HTML else {}
        rendered = exporter.render(hostile_report, report_format, **kwargs)
        if report_format in (ResearchReportFormat.DOCX, ResearchReportFormat.XLSX):
            with ZipFile(BytesIO(rendered.content), "r") as package:
                exposed = b"\n".join(package.read(name) for name in package.namelist())
            text = exposed.decode("utf-8", errors="ignore")
        else:
            text = rendered.content.decode()
        assert "http-source" in text or report_format is ResearchReportFormat.XLSX
        for canary in canaries:
            assert canary not in text


def test_all_formats_are_byte_deterministic_for_same_report() -> None:
    exporter = ResearchReportExporter()
    report = _report()

    for report_format in ResearchReportFormat:
        kwargs = {"language_tag": "uk"} if report_format is ResearchReportFormat.HTML else {}
        first = exporter.render(report, report_format, **kwargs)
        second = exporter.render(report, report_format, **kwargs)
        assert first.content == second.content
        assert first.sha256 == second.sha256


def test_office_export_rejects_non_iso_created_at() -> None:
    report = _report()
    invalid = AccessibleResearchReport(
        result_set_id=report.result_set_id,
        workspace_id=report.workspace_id,
        query=report.query,
        created_at="not-a-timestamp",
        cards=report.cards,
        text=report.text,
    )

    for report_format in (ResearchReportFormat.DOCX, ResearchReportFormat.XLSX):
        with pytest.raises(ValueError, match="ISO-8601"):
            ResearchReportExporter().render(invalid, report_format)


def test_exporter_rejects_untyped_format() -> None:
    with pytest.raises(TypeError, match="ResearchReportFormat"):
        ResearchReportExporter().render(_report(), "txt")  # type: ignore[arg-type]


def test_html_requires_explicit_valid_language_tag() -> None:
    exporter = ResearchReportExporter()
    with pytest.raises(ValueError, match="explicit BCP47"):
        exporter.render(_report(), ResearchReportFormat.HTML)
    for invalid in ("", "uk_UA", "not a tag", "1"):
        with pytest.raises(ValueError, match="BCP47"):
            exporter.render(_report(), ResearchReportFormat.HTML, language_tag=invalid)


def test_html_does_not_treat_longer_en_prefixed_primary_tag_as_english() -> None:
    rendered = ResearchReportExporter().render(
        _report(),
        ResearchReportFormat.HTML,
        language_tag="eng",
    )
    assert '<h1 lang="en">Research results</h1>' in rendered.content.decode()


def test_xlsx_rejects_values_that_excel_would_silently_truncate() -> None:
    report = _report()
    card = report.cards[0]
    long_card = ResearchCard(
        ordinal=card.ordinal,
        document_id=card.document_id,
        title="x" * 32_768,
        snippet=card.snippet,
        rank=card.rank,
        why_matched=card.why_matched,
        evidence=card.evidence,
        review=card.review,
    )
    oversized = AccessibleResearchReport(
        result_set_id=report.result_set_id,
        workspace_id=report.workspace_id,
        query=report.query,
        created_at=report.created_at,
        cards=(long_card,),
        text=report.text,
    )

    with pytest.raises(ValueError, match="title exceeds 32767"):
        ResearchReportExporter().render(oversized, ResearchReportFormat.XLSX)


def test_xlsx_limit_is_checked_after_formula_neutralization() -> None:
    report = _report()
    formula_query = "=" + ("x" * 32_766)
    boundary = AccessibleResearchReport(
        result_set_id=report.result_set_id,
        workspace_id=report.workspace_id,
        query=formula_query,
        created_at=report.created_at,
        cards=report.cards,
        text=report.text,
    )

    with pytest.raises(ValueError, match="query exceeds 32767"):
        ResearchReportExporter().render(boundary, ResearchReportFormat.XLSX)


def test_office_export_rejects_timezone_naive_created_at() -> None:
    report = _report()
    naive = AccessibleResearchReport(
        result_set_id=report.result_set_id,
        workspace_id=report.workspace_id,
        query=report.query,
        created_at="2026-08-20T07:00:00",
        cards=report.cards,
        text=report.text,
    )

    for report_format in (ResearchReportFormat.DOCX, ResearchReportFormat.XLSX):
        with pytest.raises(ValueError, match="explicit timezone"):
            ResearchReportExporter().render(naive, report_format)
