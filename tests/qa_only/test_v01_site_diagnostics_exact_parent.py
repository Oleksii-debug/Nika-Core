from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from nika_core.interaction.playwright_adapter import PageRegistry
from nika_core.interaction.site_diagnostics import PlaywrightSiteDiagnosticsProbe

_sync_api = pytest.importorskip("playwright.sync_api")
sync_playwright = _sync_api.sync_playwright


@dataclass(slots=True)
class _Session:
    registry: PageRegistry


def _browser_executable(runtime: Any) -> str | None:
    explicit = os.environ.get("NIKA_QA_CHROMIUM_EXECUTABLE", "").strip()
    if explicit and Path(explicit).is_file():
        return explicit

    managed = Path(str(runtime.chromium.executable_path))
    if managed.is_file():
        return str(managed)

    for command in (
        "chromium",
        "chromium-browser",
        "google-chrome",
        "google-chrome-stable",
    ):
        resolved = shutil.which(command)
        if resolved:
            return resolved
    return None


@pytest.fixture
def real_page() -> Iterator[tuple[Any, _Session, str]]:
    runtime = sync_playwright().start()
    executable = _browser_executable(runtime)
    if executable is None:
        runtime.stop()
        pytest.skip("local Chromium executable is unavailable for the Playwright QA fixture")

    browser = runtime.chromium.launch(headless=True, executable_path=executable)
    context = browser.new_context()
    context.set_default_timeout(1000)
    page = context.new_page()
    registry = PageRegistry(context)
    page_id = registry.register(page)
    try:
        yield page, _Session(registry=registry), page_id
    finally:
        context.close()
        browser.close()
        runtime.stop()


def test_real_playwright_relative_identities_are_resolved_and_redacted(
    real_page: tuple[Any, _Session, str],
) -> None:
    page, session, page_id = real_page
    page.set_content(
        """
        <!doctype html>
        <html lang="en">
          <head>
            <base href="https://fixture.invalid/root/">
            <title>Controlled diagnostics page</title>
          </head>
          <body>
            <form aria-label="Relative form" method="post"
                  action="../submit?token=FORM_CANARY#form-fragment"></form>
            <iframe name="child" title="Relative child"
                    src="../frame?secret=FRAME_CANARY#frame-fragment"></iframe>
          </body>
        </html>
        """,
        wait_until="domcontentloaded",
    )

    model = PlaywrightSiteDiagnosticsProbe(session, page_id).capture()  # type: ignore[arg-type]

    assert model.forms[0].action == "https://fixture.invalid/submit"
    assert model.frames[0].src == "https://fixture.invalid/frame"
    rendered = repr(model)
    assert "FORM_CANARY" not in rendered
    assert "FRAME_CANARY" not in rendered
    assert "form-fragment" not in rendered
    assert "frame-fragment" not in rendered


def test_real_playwright_shadow_scan_is_bounded_to_first_500_elements(
    real_page: tuple[Any, _Session, str],
) -> None:
    page, session, page_id = real_page
    page.set_content("<!doctype html><html><head></head><body></body></html>")
    page.evaluate(
        """
        () => {
          for (let index = 0; index < 600; index += 1) {
            const node = document.createElement('div');
            node.id = `node-${index}`;
            document.body.appendChild(node);
          }
          document.getElementById('node-0').attachShadow({mode: 'open'});
          document.getElementById('node-550').attachShadow({mode: 'open'});
        }
        """
    )

    model = PlaywrightSiteDiagnosticsProbe(session, page_id).capture()  # type: ignore[arg-type]

    assert model.shadow_scan_truncated is True
    assert model.shadow_root_count == 1, (
        "the probe scanned beyond its 500-element budget and observed the late shadow root"
    )
