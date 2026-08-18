from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser


@dataclass(frozen=True, slots=True)
class AccessibilityFinding:
    code: str
    message: str


class _AuditParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.findings: list[AccessibilityFinding] = []
        self._labels_for: set[str] = set()
        self._form_controls: list[tuple[str, str | None, str | None]] = []
        self._button_depth = 0
        self._button_has_text = False
        self.has_main = False
        self.has_live_region = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "main":
            self.has_main = True
        if values.get("aria-live") in {"polite", "assertive"} or values.get("role") in {
            "status",
            "alert",
        }:
            self.has_live_region = True
        if tag == "label" and values.get("for"):
            self._labels_for.add(values["for"] or "")
        if tag in {"input", "textarea", "select"}:
            self._form_controls.append((tag, values.get("id"), values.get("aria-label")))
        if tag == "button":
            self._button_depth += 1
            self._button_has_text = bool(values.get("aria-label"))

    def handle_data(self, data: str) -> None:
        if self._button_depth and data.strip():
            self._button_has_text = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "button" and self._button_depth:
            if not self._button_has_text:
                self.findings.append(
                    AccessibilityFinding("button-name", "Button has no accessible text or aria-label")
                )
            self._button_depth -= 1
            self._button_has_text = False

    def finish(self) -> tuple[AccessibilityFinding, ...]:
        for tag, element_id, aria_label in self._form_controls:
            if not aria_label and (not element_id or element_id not in self._labels_for):
                self.findings.append(
                    AccessibilityFinding("form-label", f"{tag} is missing an accessible label")
                )
        if not self.has_main:
            self.findings.append(AccessibilityFinding("main-landmark", "Document has no main landmark"))
        if not self.has_live_region:
            self.findings.append(
                AccessibilityFinding("live-status", "Document has no live status or alert region")
            )
        return tuple(self.findings)


def audit_html_accessibility(source: str) -> tuple[AccessibilityFinding, ...]:
    """Fast source-level gate; it does not replace UIA/NVDA human verification."""

    parser = _AuditParser()
    parser.feed(source)
    parser.close()
    return parser.finish()
