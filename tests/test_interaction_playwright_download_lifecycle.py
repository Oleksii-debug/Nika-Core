from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from nika_core.interaction import (
    BrowserSession,
    ControlNode,
    InteractionAction,
    InteractionTarget,
    PlaywrightInteractionAdapter,
    SemanticSnapshot,
    UnsupportedInteractionError,
)
from nika_core.interaction.domain import BrowserContextIdentity


class _Page:
    def __init__(self) -> None:
        self.main_frame = self
        self.frames = [self]

    def on(self, _event: str, _callback: Any) -> None:
        pass

    def is_closed(self) -> bool:
        return False


class _Context:
    def __init__(self, page: _Page) -> None:
        self.pages = [page]
        self.handlers: dict[str, Any] = {}
        self.in_download_callback = False
        self.closed = False

    def on(self, event: str, callback: Any) -> None:
        self.handlers[event] = callback

    def set_default_timeout(self, _timeout: float) -> None:
        pass

    def close(self) -> None:
        self.closed = True

    def emit_download(self, download: _Download) -> None:
        self.in_download_callback = True
        try:
            self.handlers["download"](download)
        finally:
            self.in_download_callback = False


class _Download:
    def __init__(self, context: _Context, page: _Page, *, fails: bool = False) -> None:
        self.context = context
        self.page = page
        self.suggested_filename = "доказ.txt"
        self.fails = fails
        self.attempts = 0
        self.on_save = lambda: None

    def save_as(self, destination: str) -> None:
        self.attempts += 1
        assert not self.context.in_download_callback, "save must join the caller's control flow"
        assert not self.context.closed, "save must finish before browser teardown"
        self.on_save()
        if self.fails:
            raise RuntimeError("download canceled: PRIVATE_URL_CANARY")
        Path(destination).write_text("complete UTF-8 evidence", encoding="utf-8")


@pytest.fixture
def browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    page = _Page()
    context = _Context(page)
    fake_browser = SimpleNamespace(new_context=lambda **_: context, close=lambda: None)
    fake_playwright = SimpleNamespace(
        chromium=SimpleNamespace(launch=lambda **_: fake_browser), stop=lambda: None
    )
    # Core tests do not require the optional Playwright package or a browser installation.
    sync_api = ModuleType("playwright.sync_api")
    sync_api.sync_playwright = lambda: SimpleNamespace(start=lambda: fake_playwright)
    monkeypatch.setitem(sys.modules, "playwright", ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    session = BrowserSession(download_root=tmp_path).start()
    adapter = PlaywrightInteractionAdapter(session, session.page_ids()[0])
    state = SimpleNamespace(on_click=lambda: None)
    locator = SimpleNamespace(click=lambda: state.on_click())
    monkeypatch.setattr(PlaywrightInteractionAdapter, "_locator_for_node", lambda *_: locator)
    node = ControlNode("download-link", "link", "Завантажити доказ")
    identity = BrowserContextIdentity(session.session_id, session.context_id, adapter.page_id, 1)
    snapshot = SemanticSnapshot(InteractionTarget(browser=identity), 1, 1, (node,))
    yield SimpleNamespace(
        page=page, context=context, session=session, adapter=adapter, state=state,
        node=node, snapshot=snapshot,
    )
    session.close()


def _invoke(browser: Any) -> None:
    browser.adapter.act(browser.node, InteractionAction.INVOKE, None)


def _verify(browser: Any, *, changed: str = "none") -> bool:
    after = browser.snapshot
    if changed == "revision":
        after = replace(after, revision=2)
    elif changed == "navigation":
        identity = replace(after.target.browser, document_generation=2)
        after = replace(after, target=InteractionTarget(browser=identity), generation=2)
    return browser.adapter.verify(
        browser.snapshot, after, browser.node, InteractionAction.INVOKE, None
    )


def test_download_is_joined_before_success_and_teardown(browser: Any) -> None:
    download = _Download(browser.context, browser.page)
    browser.state.on_click = lambda: browser.context.emit_download(download)
    _invoke(browser)
    assert download.attempts == 0
    assert browser.session.downloads.saved == []
    assert _verify(browser)
    assert browser.session.downloads.saved[0].read_text(encoding="utf-8") == (
        "complete UTF-8 evidence"
    )
    assert _verify(browser)
    assert download.attempts == 1
    browser.session.close()
    assert browser.session.downloads.saved[0].is_file()


@pytest.mark.parametrize("changed", ["none", "revision", "navigation"])
def test_failed_download_cannot_be_successful_or_silently_retried(browser: Any, changed: str) -> None:
    download = _Download(browser.context, browser.page, fails=True)
    browser.state.on_click = lambda: browser.context.emit_download(download)
    _invoke(browser)
    for _ in range(2):
        with pytest.raises(UnsupportedInteractionError, match="download could not be saved") as error:
            _verify(browser, changed=changed)
        assert "PRIVATE_URL_CANARY" not in str(error.value)
    assert download.attempts == 1
    assert browser.session.downloads.saved == []


def test_other_page_download_does_not_verify_this_action(browser: Any) -> None:
    download = _Download(browser.context, _Page())
    browser.state.on_click = lambda: browser.context.emit_download(download)
    _invoke(browser)
    assert not _verify(browser)
    assert download.attempts == 0
    assert browser.session.downloads.saved == []


def test_download_captured_before_action_is_not_its_evidence(browser: Any) -> None:
    download = _Download(browser.context, browser.page)
    browser.context.emit_download(download)
    _invoke(browser)
    assert not _verify(browser)
    assert download.attempts == 0


def test_previous_action_download_cannot_verify_a_later_action(browser: Any) -> None:
    download = _Download(browser.context, browser.page)
    browser.state.on_click = lambda: browser.context.emit_download(download)
    _invoke(browser)
    assert _verify(browser)
    browser.state.on_click = lambda: None
    _invoke(browser)
    assert not _verify(browser)
    assert download.attempts == 1


@pytest.mark.parametrize("changed", ["none", "revision", "navigation"])
def test_download_arriving_during_save_is_joined_before_success(browser: Any, changed: str) -> None:
    first = _Download(browser.context, browser.page)
    second = _Download(browser.context, browser.page, fails=True)
    first.on_save = lambda: browser.context.emit_download(second)
    browser.state.on_click = lambda: browser.context.emit_download(first)
    _invoke(browser)
    with pytest.raises(UnsupportedInteractionError, match="download could not be saved"):
        _verify(browser, changed=changed)
    assert (first.attempts, second.attempts) == (1, 1)


def test_continuous_download_stream_fails_closed_at_a_finite_limit(browser: Any) -> None:
    emitted: list[_Download] = []

    def emit_next() -> None:
        download = _Download(browser.context, browser.page)
        download.on_save = emit_next
        emitted.append(download)
        browser.context.emit_download(download)

    browser.state.on_click = emit_next
    _invoke(browser)
    with pytest.raises(UnsupportedInteractionError, match="download limit"):
        _verify(browser, changed="revision")
    attempts = sum(download.attempts for download in emitted)
    assert 1 < attempts <= 100
    assert emitted[-1].attempts == 0
    with pytest.raises(UnsupportedInteractionError, match="download limit"):
        _verify(browser)
    assert sum(download.attempts for download in emitted) == attempts
