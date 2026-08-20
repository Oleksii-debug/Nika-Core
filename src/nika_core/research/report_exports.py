from __future__ import annotations

import csv
import html
from dataclasses import dataclass
from enum import StrEnum
from io import BytesIO, StringIO

from docx import Document
from openpyxl import Workbook

from nika_core.research.review import AccessibleResearchReport, ResearchCard

_FORMULA_PREFIXES = ("=", "+", "-", "@")


class ResearchReportFormat(StrEnum):
    TXT = "txt"
    CSV = "csv"
    HTML = "html"
    DOCX = "docx"
    XLSX = "xlsx"


@dataclass(frozen=True, slots=True)
class RenderedResearchReport:
    filename: str
    media_type: str
    content: bytes


class ResearchReportExporter:
    """Render one structured research report without UI, network, or model calls."""

    def render(
        self,
        report: AccessibleResearchReport,
        report_format: ResearchReportFormat,
    ) -> RenderedResearchReport:
        if not isinstance(report_format, ResearchReportFormat):
            raise TypeError("report_format must be a ResearchReportFormat")

        filename = f"research-results-{_safe_id(report.result_set_id)}.{report_format.value}"
        if report_format is ResearchReportFormat.TXT:
            return RenderedResearchReport(filename, "text/plain; charset=utf-8", report.text.encode())
        if report_format is ResearchReportFormat.CSV:
            return RenderedResearchReport(
                filename,
                "text/csv; charset=utf-8",
                self._csv(report).encode(),
            )
        if report_format is ResearchReportFormat.HTML:
            return RenderedResearchReport(
                filename,
                "text/html; charset=utf-8",
                self._html(report).encode(),
            )
        if report_format is ResearchReportFormat.DOCX:
            return RenderedResearchReport(
                filename,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                self._docx(report),
            )
        return RenderedResearchReport(
            filename,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            self._xlsx(report),
        )

    def _csv(self, report: AccessibleResearchReport) -> str:
        output = StringIO(newline="")
        fieldnames = _fieldnames()
        writer = csv.DictWriter(output, fieldnames=fieldnames, lineterminator="\n")
        writer.writeheader()
        for row in _rows(report):
            writer.writerow({key: _spreadsheet_safe(value) for key, value in row.items()})
        return output.getvalue()

    def _html(self, report: AccessibleResearchReport) -> str:
        parts = [
            "<!doctype html>",
            '<html lang="und">',
            "<head>",
            '<meta charset="utf-8">',
            "<title>Research results</title>",
            "</head>",
            "<body>",
            "<main>",
            "<h1>Research results</h1>",
            f"<p><strong>Query:</strong> {_escape(report.query)}</p>",
            f"<p><strong>Created:</strong> {_escape(report.created_at)}</p>",
            f"<p><strong>Results:</strong> {len(report.cards)}</p>",
        ]
        for position, card in enumerate(report.cards, start=1):
            parts.extend(self._html_card(position, card))
        parts.extend(["</main>", "</body>", "</html>", ""])
        return "\n".join(parts)

    def _html_card(self, position: int, card: ResearchCard) -> list[str]:
        parts = [
            "<article>",
            f"<h2>Result {position}: {_escape(card.title)}</h2>",
            "<dl>",
            f"<dt>Review</dt><dd>{_escape(card.review.state.value)}</dd>",
            f"<dt>Rank</dt><dd>{card.rank}</dd>",
            f"<dt>Why matched</dt><dd>{_escape(card.why_matched)}</dd>",
            f"<dt>Summary</dt><dd>{_escape(card.snippet)}</dd>",
        ]
        if card.review.note:
            parts.append(f"<dt>Review note</dt><dd>{_escape(card.review.note)}</dd>")
        parts.append("</dl>")
        if card.evidence:
            parts.extend(["<h3>Evidence</h3>", "<ol>"])
            for evidence in card.evidence:
                freshness = evidence.freshness.value if evidence.freshness is not None else "n/a"
                parts.extend(
                    [
                        "<li><dl>",
                        f"<dt>Source ID</dt><dd>{_escape(evidence.source_id)}</dd>",
                        f"<dt>Source kind</dt><dd>{_escape(evidence.source_kind.value)}</dd>",
                        f"<dt>Freshness</dt><dd>{_escape(freshness)}</dd>",
                        f"<dt>Location</dt><dd>{_escape(evidence.locator)}</dd>",
                        f"<dt>Observed</dt><dd>{_escape(evidence.observed_at)}</dd>",
                        "</dl></li>",
                    ]
                )
            parts.append("</ol>")
        else:
            parts.append("<p><strong>Evidence:</strong> none recorded</p>")
        parts.append("</article>")
        return parts

    def _docx(self, report: AccessibleResearchReport) -> bytes:
        document = Document()
        document.core_properties.title = "Research results"
        document.add_heading("Research results", level=0)
        document.add_paragraph(f"Query: {report.query}")
        document.add_paragraph(f"Created: {report.created_at}")
        document.add_paragraph(f"Results: {len(report.cards)}")
        for position, card in enumerate(report.cards, start=1):
            document.add_heading(f"Result {position}: {card.title}", level=1)
            document.add_paragraph(f"Review: {card.review.state.value}")
            document.add_paragraph(f"Rank: {card.rank}")
            document.add_paragraph(f"Why matched: {card.why_matched}")
            document.add_paragraph(f"Summary: {card.snippet}")
            if card.review.note:
                document.add_paragraph(f"Review note: {card.review.note}")
            if card.evidence:
                document.add_heading("Evidence", level=2)
                for evidence_index, evidence in enumerate(card.evidence, start=1):
                    freshness = evidence.freshness.value if evidence.freshness is not None else "n/a"
                    paragraph = document.add_paragraph(style="List Number")
                    paragraph.add_run(f"Evidence {evidence_index}").bold = True
                    document.add_paragraph(f"Source ID: {evidence.source_id}")
                    document.add_paragraph(f"Source kind: {evidence.source_kind.value}")
                    document.add_paragraph(f"Freshness: {freshness}")
                    document.add_paragraph(f"Location: {evidence.locator}")
                    document.add_paragraph(f"Observed: {evidence.observed_at}")
            else:
                document.add_paragraph("Evidence: none recorded")
        output = BytesIO()
        document.save(output)
        return output.getvalue()

    def _xlsx(self, report: AccessibleResearchReport) -> bytes:
        workbook = Workbook()
        workbook.properties.title = "Research results"
        metadata = workbook.active
        metadata.title = "Metadata"
        metadata.append(("Field", "Value"))
        metadata.append(("Result set ID", _spreadsheet_safe(report.result_set_id)))
        metadata.append(("Workspace ID", _spreadsheet_safe(report.workspace_id)))
        metadata.append(("Query", _spreadsheet_safe(report.query)))
        metadata.append(("Created", _spreadsheet_safe(report.created_at)))
        metadata.append(("Results", len(report.cards)))
        metadata.freeze_panes = "A2"

        results = workbook.create_sheet("Results")
        fieldnames = _fieldnames()
        results.append(tuple(fieldnames))
        for row in _rows(report):
            results.append(tuple(_spreadsheet_safe(row[key]) for key in fieldnames))
        results.freeze_panes = "A2"
        results.auto_filter.ref = results.dimensions

        output = BytesIO()
        workbook.save(output)
        return output.getvalue()


def _fieldnames() -> tuple[str, ...]:
    return (
        "result_set_id",
        "workspace_id",
        "query",
        "created_at",
        "ordinal",
        "document_id",
        "title",
        "review_state",
        "review_note",
        "rank",
        "why_matched",
        "summary",
        "evidence_index",
        "source_id",
        "source_kind",
        "freshness",
        "locator",
        "observed_at",
    )


def _rows(report: AccessibleResearchReport) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for card in report.cards:
        evidence_items = card.evidence or (None,)
        for evidence_index, evidence in enumerate(evidence_items, start=1):
            row: dict[str, object] = {
                "result_set_id": report.result_set_id,
                "workspace_id": report.workspace_id,
                "query": report.query,
                "created_at": report.created_at,
                "ordinal": card.ordinal,
                "document_id": card.document_id,
                "title": card.title,
                "review_state": card.review.state.value,
                "review_note": card.review.note,
                "rank": card.rank,
                "why_matched": card.why_matched,
                "summary": card.snippet,
                "evidence_index": evidence_index if evidence is not None else "",
                "source_id": "",
                "source_kind": "",
                "freshness": "",
                "locator": "",
                "observed_at": "",
            }
            if evidence is not None:
                row.update(
                    {
                        "source_id": evidence.source_id,
                        "source_kind": evidence.source_kind.value,
                        "freshness": (
                            evidence.freshness.value if evidence.freshness is not None else "n/a"
                        ),
                        "locator": evidence.locator,
                        "observed_at": evidence.observed_at,
                    }
                )
            rows.append(row)
    return rows


def _spreadsheet_safe(value: object) -> object:
    if not isinstance(value, str):
        return value
    stripped = value.lstrip()
    if stripped.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _safe_id(value: str) -> str:
    cleaned = "".join(character if character.isalnum() or character in "-_" else "-" for character in value)
    cleaned = cleaned.strip("-_")
    return (cleaned or "report")[:64]
