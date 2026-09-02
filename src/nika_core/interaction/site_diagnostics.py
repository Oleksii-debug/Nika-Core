"""Bounded read-only browser reconnaissance for the V0.1 adaptive interaction path.

This module intentionally does not mutate page state and does not expose cookies, storage,
credentials, request headers, response bodies, query strings, fragments, or input values.
It reuses an already-owned Playwright page through :class:`BrowserSession` and produces a small,
redacted Site Model that can help a semantic-first caller decide whether to re-observe or fail
closed after an ordinary site redesign.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlsplit, urlunsplit

from .domain import StaleSnapshotError
from .playwright_adapter import BrowserSession

_MAX_TEXT: Final = 160
_MAX_CONTROLS: Final = 100
_MAX_HEADINGS: Final = 40
_MAX_FORMS: Final = 20
_MAX_FRAMES: Final = 20
_SECRET_RE: Final = re.compile(
    r"(?i)(?:bearer\s+[A-Za-z0-9._~+/-]+=*|"
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)\s*[:=]\s*\S+)"
)

_READ_ONLY_SITE_MODEL_JS: Final = r"""
() => {
  const compact = (value) => typeof value === "string"
    ? value.replace(/\s+/g, " ").trim().slice(0, 160)
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
  const controls = Array.from(document.querySelectorAll(
    "button,input,textarea,select,[role],[contenteditable='true']"
  )).slice(0, 100).map((el) => ({
    tag: compact(el.tagName.toLowerCase()),
    role: roleFor(el),
    name: nameFor(el),
    enabled: !(el.disabled || el.getAttribute("aria-disabled") === "true"),
    visible: visible(el),
    contenteditable: el.getAttribute("contenteditable") === "true"
  }));
  const headings = Array.from(document.querySelectorAll("h1,h2,h3,h4,h5,h6,[role='heading']"))
    .slice(0, 40)
    .map((el) => ({
      level: Number(el.getAttribute("aria-level") || el.tagName.slice(1) || 0),
      text: compact(el.textContent || "")
    }));
  const forms = Array.from(document.forms).slice(0, 20).map((form) => ({
    name: compact(form.getAttribute("aria-label") || form.getAttribute("name") || ""),
    method: compact((form.getAttribute("method") || "get").toLowerCase()),
    action: compact(form.getAttribute("action") || "")
  }));
  const frames = Array.from(document.querySelectorAll("iframe")).slice(0, 20).map((frame) => ({
    name: compact(frame.getAttribute("name") || ""),
    title: compact(frame.getAttribute("title") || ""),
    src: compact(frame.getAttribute("src") || "")
  }));
  let shadowRootCount = 0;
  for (const el of Array.from(document.querySelectorAll("*"))) {
    if (el.shadowRoot) shadowRootCount += 1;
  }
  return {
    url: String(globalThis.location.href),
    title: compact(document.title),
    ready_state: String(document.readyState),
    controls,
    headings,
    forms,
    frames,
    shadow_root_count: shadowRootCount
  };
}
"""


def _safe_text(value: object, *, limit: int = _MAX_TEXT) -> str:
    if not isinstance(value, str):
        return ""
    compact = " ".join(value.split())
    redacted = _SECRET_RE.sub("[redacted]", compact)
    return redacted[:limit]


def _safe_url(value: object) -> str:
    """Return scheme/host/path only; drop credentials, query and fragment."""
    text = _safe_text(value, limit=1024)
    if not text:
        return ""
    try:
        parsed = urlsplit(text)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"}:
        return ""
    if not parsed.hostname:
        return ""
    host = parsed.hostname
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    path = parsed.path[:256]
    return urlunsplit((parsed.scheme, host, path, "", ""))


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"site diagnostics {field} is malformed")
    return value


def _sequence(value: object, field: str, maximum: int) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValueError(f"site diagnostics {field} is malformed")
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
        payload = record.page.evaluate(_READ_ONLY_SITE_MODEL_JS)
        data = _mapping(payload, "payload")

        controls: list[SiteControlEvidence] = []
        for raw in _sequence(data.get("controls", ()), "controls", _MAX_CONTROLS):
            item = _mapping(raw, "control")
            controls.append(
                SiteControlEvidence(
                    tag=_safe_text(item.get("tag")),
                    role=_safe_text(item.get("role")),
                    name=_safe_text(item.get("name")),
                    enabled=bool(item.get("enabled", False)),
                    visible=bool(item.get("visible", False)),
                    contenteditable=bool(item.get("contenteditable", False)),
                )
            )

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
            forms.append(
                SiteFormEvidence(
                    name=_safe_text(item.get("name")),
                    method=method,
                    action=_safe_url(item.get("action")),
                )
            )

        frames: list[SiteFrameEvidence] = []
        for raw in _sequence(data.get("frames", ()), "frames", _MAX_FRAMES):
            item = _mapping(raw, "frame")
            frames.append(
                SiteFrameEvidence(
                    name=_safe_text(item.get("name")),
                    title=_safe_text(item.get("title")),
                    src=_safe_url(item.get("src")),
                )
            )

        shadow_count = data.get("shadow_root_count", 0)
        if isinstance(shadow_count, bool) or not isinstance(shadow_count, int) or shadow_count < 0:
            raise ValueError("site diagnostics shadow_root_count is malformed")

        ready_state = _safe_text(data.get("ready_state")).casefold()
        if ready_state not in {"loading", "interactive", "complete"}:
            ready_state = "unknown"

        return SiteModel(
            page_id=self.page_id,
            document_generation=record.document_generation,
            url=_safe_url(data.get("url")),
            title=_safe_text(data.get("title")),
            ready_state=ready_state,
            controls=tuple(controls),
            headings=tuple(headings),
            forms=tuple(forms),
            frames=tuple(frames),
            shadow_root_count=shadow_count,
        )
