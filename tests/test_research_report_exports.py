from __future__ import annotations

import csv
from io import BytesIO, StringIO

from docx import Document
from openpyxl import load_workbook

from nika_core.research.models import FreshnessState, ResearchEvidence, SourceKind
from nika_core.research.report_exports import ResearchReportExporter, ResearchReportFormat
from nika_core.research.review import (
    AccessibleResearchReport,
    ResearchCard,
    ResearchReview,
    ResearchReviewState,
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
            note="@SUM(A1:A2) перевірити вручну",
            updated_at="2026-08-20T07:10:00+00:00",
        ),
    )
    return AccessibleResearchReport(
        result_set_id="results/unsafe id",
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


def test_txt_export_is_utf8_and_uses_safe_generated_filename() -> None:
    rendered = ResearchReportExporter().render(_report(), ResearchReportFormat.TXT)

    assert rendered.filename == "research-results-results-unsafe-id.txt"
    assert rendered.media_type == "text/plain; charset=utf-8"
    assert rendered.content.decode() == _report().text


def test_csv_export_is_lossless_for_provenance_and_blocks_formula_injection() -> None:
    rendered = ResearchReportExporter().render(_report(), ResearchReportFormat.CSV)
    rows = list(csv.DictReader(StringIO(rendered.content.decode())))

    assert len(rows) == 1
    row = rows[0]
    assert row["title"] == "'=1+1 Українська <можливість>"
    assert row["review_note"] == "'@SUM(A1:A2) перевірити вручну"
    assert row["source_id"] == "source-1"
    assert row["source_kind"] == "http"
    assert row["freshness"] == "current"
    assert row["locator"] == "https://example.org/?a=1&b=2"
    assert row["observed_at"] == "2026-08-20T07:00:00+00:00"


def test_html_export_uses_semantic_structure_and_escapes_untrusted_text() -> None:
    rendered = ResearchReportExporter().render(_report(), ResearchReportFormat.HTML)
    text = rendered.content.decode()

    assert '<html lang="und">' in text
    assert "<main>" in text
    assert "<h1>Research results</h1>" in text
    assert "<h2>Result 1: =1+1 Українська &lt;можливість&gt;</h2>" in text
    assert "Грант &amp; навчання &lt;script&gt;alert(1)&lt;/script&gt;" in text
    assert "<ol>" in text
    assert "https://example.org/?a=1&amp;b=2" in text
    assert "<script>alert(1)</script>" not in text


def test_docx_export_preserves_headings_review_and_provenance() -> None:
    rendered = ResearchReportExporter().render(_report(), ResearchReportFormat.DOCX)
    document = Document(BytesIO(rendered.content))
    paragraphs = [paragraph.text for paragraph in document.paragraphs]

    assert "Research results" in paragraphs
    assert "Result 1: =1+1 Українська <можливість>" in paragraphs
    assert "Review: saved" in paragraphs
    assert "Review note: @SUM(A1:A2) перевірити вручну" in paragraphs
    assert "Source ID: source-1" in paragraphs
    assert "Location: https://example.org/?a=1&b=2" in paragraphs


def test_xlsx_export_is_flat_accessible_and_blocks_formula_injection() -> None:
    rendered = ResearchReportExporter().render(_report(), ResearchReportFormat.XLSX)
    workbook = load_workbook(BytesIO(rendered.content), data_only=False)

    assert workbook.sheetnames == ["Metadata", "Results"]
    assert workbook["Metadata"]["A1"].value == "Field"
    assert workbook["Results"].freeze_panes == "A2"
    headers = [cell.value for cell in workbook["Results"][1]]
    row = [cell.value for cell in workbook["Results"][2]]
    values = dict(zip(headers, row, strict=True))
    assert values["title"] == "'=1+1 Українська <можливість>"
    assert values["review_note"] == "'@SUM(A1:A2) перевірити вручну"
    assert values["source_id"] == "source-1"
    assert values["locator"] == "https://example.org/?a=1&b=2"
    assert workbook["Results"]["G2"].data_type == "s"


def test_exporter_rejects_untyped_format() -> None:
    exporter = ResearchReportExporter()

    try:
        exporter.render(_report(), "txt")  # type: ignore[arg-type]
    except TypeError as exc:
        assert "ResearchReportFormat" in str(exc)
    else:
        raise AssertionError("untyped report format must fail closed")
