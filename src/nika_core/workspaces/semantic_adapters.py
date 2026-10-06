from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .accessibility_repair import AccessibilityEvidence, EvidenceMethod


@dataclass(slots=True)
class PlaywrightSemanticAdapter:
    """Browser semantic inspector backed by Playwright accessibility snapshots."""

    timeout_ms: float = 10_000

    async def inspect_browser(self, target: str) -> AccessibilityEvidence:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - optional dependency boundary
            raise RuntimeError("Playwright browser component is not installed") from exc

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                page.set_default_timeout(self.timeout_ms)
                if target.startswith("html:"):
                    await page.set_content(target.removeprefix("html:"))
                    evidence_target = "inline-html"
                else:
                    await page.goto(target, wait_until="domcontentloaded")
                    evidence_target = target
                snapshot = await page.locator("body").aria_snapshot()
            finally:
                await browser.close()

        controls = tuple(
            line.strip().removeprefix("- ")
            for line in snapshot.splitlines()
            if line.lstrip().startswith("-")
        )
        return AccessibilityEvidence(
            target=evidence_target,
            method=EvidenceMethod.DOM,
            summary=snapshot,
            accessible_controls=controls,
            confidence=1.0,
        )

    async def inspect_windows(self, target: str) -> AccessibilityEvidence:
        raise RuntimeError("PlaywrightSemanticAdapter handles browser targets only")


@dataclass(slots=True)
class PywinautoUIAAdapter:
    """Windows semantic inspector backed by Microsoft UI Automation via pywinauto."""

    process_id: int

    async def inspect_windows(self, target: str) -> AccessibilityEvidence:
        return await asyncio.to_thread(self._inspect_sync, target)

    def _inspect_sync(self, target: str) -> AccessibilityEvidence:
        try:
            from pywinauto.application import Application
        except ImportError as exc:  # pragma: no cover - optional dependency boundary
            raise RuntimeError("pywinauto Windows interaction component is not installed") from exc

        app = Application(backend="uia").connect(process=self.process_id)
        window = app.top_window().wrapper_object()
        wrappers = (window, *window.descendants())
        controls: list[str] = []
        for wrapper in wrappers:
            info = wrapper.element_info
            name = str(getattr(info, "name", "") or "").strip()
            control_type = str(getattr(info, "control_type", "") or "").strip()
            if name or control_type:
                controls.append(f"{control_type}:{name}" if name else control_type)
        return AccessibilityEvidence(
            target=target,
            method=EvidenceMethod.UIA,
            summary="\n".join(controls),
            accessible_controls=tuple(controls),
            confidence=1.0,
        )

    async def inspect_browser(self, target: str) -> AccessibilityEvidence:
        raise RuntimeError("PywinautoUIAAdapter handles Windows targets only")
