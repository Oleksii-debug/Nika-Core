from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from nika_core.interaction import (
    AmbiguousTargetError,
    ControlLocator,
    InteractionAction,
    StaleSnapshotError,
    TargetNotFoundError,
    UnsupportedInteractionError,
    resolve_strict,
)
from nika_core.interaction.playwright_adapter import (
    FrameScope,
    PageRegistry,
    PlaywrightInteractionAdapter,
)

_sync_api = pytest.importorskip("playwright.sync_api")
PlaywrightTimeoutError = _sync_api.TimeoutError
sync_playwright = _sync_api.sync_playwright

_TARGET = ControlLocator(role="button", name="Run target")


@dataclass(slots=True)
class _Downloads:
    saved: list[Path] = field(default_factory=list)


@dataclass(slots=True)
class _LocalSession:
    context: Any
    registry: PageRegistry
    session_id: str = "w35-local-session"
    context_id: str = "w35-local-context"
    downloads: _Downloads = field(default_factory=_Downloads)

    def page_ids(self) -> tuple[str, ...]:
        return tuple(self.registry.pages)


@dataclass(slots=True)
class _BrowserFixture:
    runtime: Any
    browser: Any
    context: Any
    page: Any
    session: _LocalSession
    page_id: str

    def adapter(self, *, frame_scope: FrameScope | None = None) -> PlaywrightInteractionAdapter:
        return PlaywrightInteractionAdapter(
            session=self.session,
            page_id=self.page_id,
            frame_scope=frame_scope,
        )

    def close(self) -> None:
        try:
            self.context.close()
            self.browser.close()
        finally:
            self.runtime.stop()


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
def browser_fixture() -> _BrowserFixture:
    runtime = sync_playwright().start()
    executable = _browser_executable(runtime)
    if executable is None:
        runtime.stop()
        pytest.skip("local Chromium executable is unavailable for the Playwright QA fixture")

    browser = runtime.chromium.launch(headless=True, executable_path=executable)
    context = browser.new_context()
    context.set_default_timeout(500)
    registry = PageRegistry(context)
    session = _LocalSession(context=context, registry=registry)
    page = context.new_page()
    page_id = registry.register(page)
    fixture = _BrowserFixture(
        runtime=runtime,
        browser=browser,
        context=context,
        page=page,
        session=session,
        page_id=page_id,
    )
    try:
        yield fixture
    finally:
        fixture.close()


def _button_html(effect: str) -> str:
    return f"""
    <!doctype html>
    <html lang="en">
      <body>
        <script>window.__effects = [];</script>
        <main>
          <section aria-label="Task controls">
            <button id="target" aria-label="Run target"
                    onclick="window.__effects.push('{effect}')">Run target</button>
          </section>
        </main>
      </body>
    </html>
    """


def _effects(page: Any) -> list[str]:
    return list(page.evaluate("() => [...window.__effects]"))


def _resolve(adapter: PlaywrightInteractionAdapter) -> Any:
    return resolve_strict(adapter.observe(), _TARGET)


def _invoke(adapter: PlaywrightInteractionAdapter, node: Any) -> None:
    adapter.act(node, InteractionAction.INVOKE, None)


def _require_stale_and_no_effect(
    adapter: PlaywrightInteractionAdapter,
    node: Any,
    page: Any,
) -> None:
    try:
        _invoke(adapter, node)
    except StaleSnapshotError:
        pass
    else:
        assert _effects(page) == [], "stale action reached an unintended replacement target"
        pytest.fail("stale observed node remained executable after target identity changed")
    assert _effects(page) == []


def test_same_semantic_node_replacement_cannot_receive_old_action(
    browser_fixture: _BrowserFixture,
) -> None:
    adapter = browser_fixture.adapter()
    adapter.load_inline_fixture(_button_html("original"))
    stale_node = _resolve(adapter)

    browser_fixture.page.evaluate(
        """
        () => {
          const oldTarget = document.getElementById('target');
          const replacement = document.createElement('button');
          replacement.id = 'target';
          replacement.setAttribute('aria-label', 'Run target');
          replacement.textContent = 'Run target';
          replacement.addEventListener(
            'click',
            () => window.__effects.push('replacement'),
          );
          oldTarget.replaceWith(replacement);
        }
        """
    )

    _require_stale_and_no_effect(adapter, stale_node, browser_fixture.page)

    fresh_node = _resolve(adapter)
    _invoke(adapter, fresh_node)
    assert _effects(browser_fixture.page) == ["replacement"]


@pytest.mark.parametrize(
    ("mutation", "allowed_errors"),
    (
        (
            "document.getElementById('target').setAttribute('aria-label', 'Changed target')",
            (StaleSnapshotError, TargetNotFoundError),
        ),
        (
            "document.getElementById('target').disabled = true",
            (StaleSnapshotError, UnsupportedInteractionError, PlaywrightTimeoutError),
        ),
        (
            "document.getElementById('target').hidden = true",
            (StaleSnapshotError, TargetNotFoundError, PlaywrightTimeoutError),
        ),
        (
            "document.getElementById('target').setAttribute('role', 'link')",
            (StaleSnapshotError, TargetNotFoundError),
        ),
    ),
    ids=("accessible-name", "disabled", "hidden", "role"),
)
def test_semantic_or_actionability_drift_fails_closed_without_effect(
    browser_fixture: _BrowserFixture,
    mutation: str,
    allowed_errors: tuple[type[BaseException], ...],
) -> None:
    adapter = browser_fixture.adapter()
    adapter.load_inline_fixture(_button_html("original"))
    stale_node = _resolve(adapter)

    browser_fixture.page.evaluate(f"() => {{ {mutation}; }}")

    with pytest.raises(allowed_errors):
        _invoke(adapter, stale_node)
    assert _effects(browser_fixture.page) == []


def test_duplicate_added_after_resolution_remains_fail_closed(
    browser_fixture: _BrowserFixture,
) -> None:
    adapter = browser_fixture.adapter()
    adapter.load_inline_fixture(_button_html("original"))
    stale_node = _resolve(adapter)

    browser_fixture.page.evaluate(
        """
        () => {
          const duplicate = document.createElement('button');
          duplicate.setAttribute('aria-label', 'Run target');
          duplicate.textContent = 'Run target';
          duplicate.addEventListener(
            'click',
            () => window.__effects.push('duplicate'),
          );
          document.querySelector('main').appendChild(duplicate);
        }
        """
    )

    with pytest.raises((StaleSnapshotError, AmbiguousTargetError)):
        _invoke(adapter, stale_node)
    assert _effects(browser_fixture.page) == []

    with pytest.raises(AmbiguousTargetError):
        resolve_strict(adapter.observe(), _TARGET)
    assert _effects(browser_fixture.page) == []


def test_same_name_frame_replacement_cannot_receive_old_action(
    browser_fixture: _BrowserFixture,
) -> None:
    page_adapter = browser_fixture.adapter()
    page_adapter.load_inline_fixture(
        """
        <!doctype html>
        <html lang="en">
          <body>
            <script>window.__effects = [];</script>
            <main>
              <iframe id="host-frame" name="work-frame" title="Work frame"></iframe>
            </main>
          </body>
        </html>
        """
    )
    browser_fixture.page.evaluate(
        """
        () => {
          document.getElementById('host-frame').srcdoc = `
            <!doctype html><html lang="en"><body>
              <button id="inside" aria-label="Run target"
                      onclick="parent.__effects.push('original')">Run target</button>
            </body></html>`;
        }
        """
    )
    browser_fixture.page.wait_for_function(
        """
        () => {
          const frame = document.getElementById('host-frame');
          return frame?.contentDocument?.getElementById('inside') !== null;
        }
        """
    )

    adapter = browser_fixture.adapter(frame_scope=FrameScope(name="work-frame"))
    stale_node = _resolve(adapter)

    browser_fixture.page.evaluate(
        """
        () => {
          const oldFrame = document.getElementById('host-frame');
          const replacement = document.createElement('iframe');
          replacement.id = 'host-frame';
          replacement.name = 'work-frame';
          replacement.title = 'Work frame';
          replacement.srcdoc = `
            <!doctype html><html lang="en"><body>
              <button id="inside" aria-label="Run target"
                      onclick="parent.__effects.push('replacement')">Run target</button>
            </body></html>`;
          oldFrame.replaceWith(replacement);
        }
        """
    )
    browser_fixture.page.wait_for_function(
        """
        () => {
          const frame = document.getElementById('host-frame');
          return frame?.contentDocument?.getElementById('inside') !== null;
        }
        """
    )

    _require_stale_and_no_effect(adapter, stale_node, browser_fixture.page)

    fresh_node = _resolve(adapter)
    _invoke(adapter, fresh_node)
    assert _effects(browser_fixture.page) == ["replacement"]


def test_page_generation_replacement_cannot_receive_old_action(
    browser_fixture: _BrowserFixture,
) -> None:
    adapter = browser_fixture.adapter()
    adapter.load_inline_fixture(_button_html("original"))
    stale_node = _resolve(adapter)

    adapter.load_inline_fixture(_button_html("replacement"))

    _require_stale_and_no_effect(adapter, stale_node, browser_fixture.page)

    fresh_node = _resolve(adapter)
    _invoke(adapter, fresh_node)
    assert _effects(browser_fixture.page) == ["replacement"]
