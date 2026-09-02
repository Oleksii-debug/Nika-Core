"""Bounded read-only browser reconnaissance for the V0.1 adaptive interaction path."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit, urlunsplit

from .domain import StaleSnapshotError
from .playwright_adapter import BrowserSession

_MAX_TEXT: Final = 160
_MAX_CONTROLS: Final = 100
_MAX_HEADINGS: Final = 40
_MAX_FORMS: Final = 20
_MAX_FRAMES: Final = 20
_MAX_SHADOW_SCAN: Final = 500
_SECRET_RE: Final = re.compile(
    r"(?i)(?:bearer\s+[A-Za-z0-9._~+/-]+=*|"
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)\s*[:=]\s*\S+)"
)

_READ_ONLY_SITE_MODEL_JS: Final = r"""
() => {
  const compact = (value) => typeof value === "string"
    ? value.slice(0, 1024).replace(/\s+/g, " ").trim().slice(0, 160)
    : "";
  const visible = (el) => {
    const style = globalThis.getComputedStyle(el);
    if (style.visibility === "hidden" || style.display === "none") return false;
    return Boolean(el.getClientRects().length);
  };
  const roleFor = (el) => compact(el.getAttribute("role") || "");
  const nameFor = (el) => {
    const aria = compact(el.getAttribute("aria-label") || "");
    if (aria) return aria;
    if (el.labels && el.labels.length === 1) return compact(el.labels[0].textContent || "");
    return compact(el.getAttribute("title") || el.getAttribute("name") || "");
  };
  const controls = [];
  const headings = [];
  const forms = [];
  const frames = [];
  let shadowRootCount = 0;
  let scanned = 0;
  let scanTruncated = false;
  if (document.documentElement) {
    const walker = document.createTreeWalker(document.documentElement, NodeFilter.SHOW_ELEMENT);
    let el = walker.currentNode;
    while (el && scanned < 500) {
      if (el.shadowRoot) shadowRootCount += 1;
      const tag = el.tagName.toLowerCase();
      const role = roleFor(el);
      const contenteditable = el.getAttribute("contenteditable") === "true";
      if (controls.length < 100 && (
        tag === "button" || tag === "input" || tag === "textarea" || tag === "select"
        || el.hasAttribute("role") || contenteditable
      )) {
        controls.push({
          tag: compact(tag), role: compact(role), name: nameFor(el),
          enabled: !(el.disabled || el.getAttribute("aria-disabled") === "true"),
          visible: visible(el), contenteditable
        });
      }
      if (headings.length < 40 && (/^h[1-6]$/.test(tag) || role === "heading")) {
        const nativeLevel = /^h[1-6]$/.test(tag) ? tag.slice(1) : "0";
        headings.push({
          level: Number(el.getAttribute("aria-level") || nativeLevel),
          text: compact(el.textContent || "")
        });
      }
      if (forms.length < 20 && tag === "form") {
        forms.push({
          name: compact(el.getAttribute("aria-label") || el.getAttribute("name") || ""),
          method: compact((el.getAttribute("method") || "get").toLowerCase()),
          action: compact(el.action || "")
        });
      }
      if (frames.length < 20 && tag === "iframe") {
        frames.push({
          name: compact(el.getAttribute("name") || ""), title: compact(el.getAttribute("title") || ""),
          src: compact(el.src || "")
        });
      }
      scanned += 1;
      el = walker.nextNode();
    }
    scanTruncated = Boolean(el);
  }
  return {
    url: String(globalThis.location.href), title: compact(document.title),
    ready_state: String(document.readyState), controls, headings, forms, frames,
    shadow_root_count: shadowRootCount, shadow_scan_truncated: scanTruncated
  };
}
"""


def _safe_text(value: object, *, limit: int = _MAX_TEXT) -> str:
    if not isinstance(value, str):
        return ""
    compact = " ".join(value.split())
    return _SECRET_RE.sub("[redacted]", compact)[:limit]


def _safe_url(value: object) -> str:
    """Return scheme/host/path only; drop credentials, query and fragment."""
    text = _safe_text(value, limit=1024)
    if not text:
        return ""
    try:
        parsed = urlsplit(text)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    host = parsed.hostname
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme, host, parsed.path[:256], "", ""))


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError(f"site diagnostics {field} is malformed")
    return value


def _sequence(value: object, field: str, maximum: int) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError(f"site diagnostics {field} is malformed")
    if len(value) > maximum:
        raise ValueError(f"site diagnostics {field} exceeded bounded size")
    return value


@dataclass(frozen=True, slots=True)
class SiteControlEvidence:
    tag: str
    role: str
    name: str
    enabled: bool
    visible: bool
    contenteditable: bool


@dataclass(frozen=True, slots=True)
class SiteHeadingEvidence:
    level: int
    text: str


@dataclass(frozen=True, slots=True)
class SiteFormEvidence:
    name: str
    method: str
    action: str


@dataclass(frozen=True, slots=True)
class SiteFrameEvidence:
    name: str
    title: str
    src: str


@dataclass(frozen=True, slots=True)
class SiteModel:
    page_id: str
    document_generation: int
    url: str
    title: str
    ready_state: str
    controls: tuple[SiteControlEvidence, ...]
    headings: tuple[SiteHeadingEvidence, ...]
    forms: tuple[SiteFormEvidence, ...]
    frames: tuple[SiteFrameEvidence, ...]
    shadow_root_count: int
    shadow_scan_truncated: bool


@dataclass(slots=True)
class PlaywrightSiteDiagnosticsProbe:
    """Read only task-owned page metadata through the existing BrowserSession registry."""

    session: BrowserSession
    page_id: str

    def capture(self) -> SiteModel:
        registry = self.session.registry
        if registry is None:
            raise StaleSnapshotError("browser session is not started")
        record = registry.get(self.page_id)
        data = _mapping(record.page.evaluate(_READ_ONLY_SITE_MODEL_JS), "payload")

        controls: list[SiteControlEvidence] = []
        for raw in _sequence(data.get("controls", ()), "controls", _MAX_CONTROLS):
            item = _mapping(raw, "control")
            controls.append(SiteControlEvidence(
                tag=_safe_text(item.get("tag")), role=_safe_text(item.get("role")),
                name=_safe_text(item.get("name")), enabled=bool(item.get("enabled", False)),
                visible=bool(item.get("visible", False)),
                contenteditable=bool(item.get("contenteditable", False)),
            ))

        headings: list[SiteHeadingEvidence] = []
        for raw in _sequence(data.get("headings", ()), "headings", _MAX_HEADINGS):
            item = _mapping(raw, "heading")
            level = item.get("level", 0)
            if isinstance(level, bool) or not isinstance(level, int) or not 0 <= level <= 6:
                raise ValueError("site diagnostics heading level is malformed")
            headings.append(SiteHeadingEvidence(level=level, text=_safe_text(item.get("text"))))

        forms: list[SiteFormEvidence] = []
        for raw in _sequence(data.get("forms", ()), "forms", _MAX_FORMS):
            item = _mapping(raw, "form")
            method = _safe_text(item.get("method")).casefold()
            if method not in {"get", "post", "dialog"}:
                method = "unknown"
            forms.append(SiteFormEvidence(
                name=_safe_text(item.get("name")), method=method, action=_safe_url(item.get("action"))
            ))

        frames: list[SiteFrameEvidence] = []
        for raw in _sequence(data.get("frames", ()), "frames", _MAX_FRAMES):
            item = _mapping(raw, "frame")
            frames.append(SiteFrameEvidence(
                name=_safe_text(item.get("name")), title=_safe_text(item.get("title")),
                src=_safe_url(item.get("src"))
            ))

        shadow_count = data.get("shadow_root_count", 0)
        if (isinstance(shadow_count, bool) or not isinstance(shadow_count, int)
                or not 0 <= shadow_count <= _MAX_SHADOW_SCAN):
            raise ValueError("site diagnostics shadow_root_count is malformed")
        shadow_truncated = data.get("shadow_scan_truncated", False)
        if not isinstance(shadow_truncated, bool):
            raise TypeError("site diagnostics shadow_scan_truncated is malformed")

        ready_state = _safe_text(data.get("ready_state")).casefold()
        if ready_state not in {"loading", "interactive", "complete"}:
            ready_state = "unknown"

        return SiteModel(
            page_id=self.page_id, document_generation=record.document_generation,
            url=_safe_url(data.get("url")), title=_safe_text(data.get("title")),
            ready_state=ready_state, controls=tuple(controls), headings=tuple(headings),
            forms=tuple(forms), frames=tuple(frames), shadow_root_count=shadow_count,
            shadow_scan_truncated=shadow_truncated,
        )
