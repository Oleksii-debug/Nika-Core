from __future__ import annotations

from dataclasses import dataclass

import pytest

from nika_core.interaction.site_diagnostics import PlaywrightSiteDiagnosticsProbe


@dataclass
class _Record:
    page: object
    document_generation: int = 7


class _Page:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.scripts: list[str] = []

    def evaluate(self, script: str) -> object:
        self.scripts.append(script)
        return self.payload


class _Registry:
    def __init__(self, page: _Page) -> None:
        self.page = page

    def get(self, page_id: str) -> _Record:
        assert page_id == "page-1"
        return _Record(self.page)


class _Session:
    def __init__(self, page: _Page) -> None:
        self.registry = _Registry(page)


def _capture(payload: object):
    page = _Page(payload)
    model = PlaywrightSiteDiagnosticsProbe(_Session(page), "page-1").capture()  # type: ignore[arg-type]
    return model, page.scripts[0]


def test_site_model_is_bounded_redacted_and_drops_url_secrets() -> None:
    model, script = _capture(
        {
            "url": "https://example.test/a/path?token=QUERY_SECRET#fragment",
            "title": "Account api_key=TITLE_SECRET",
            "ready_state": "complete",
            "controls": [
                {
                    "tag": "input",
                    "role": "textbox",
                    "name": "Email access_token=CONTROL_SECRET",
                    "enabled": True,
                    "visible": True,
                    "contenteditable": False,
                }
            ],
            "headings": [{"level": 1, "text": "Dashboard bearer HEADER_SECRET"}],
            "forms": [
                {
                    "name": "Profile",
                    "method": "post",
                    "action": "https://example.test/save?secret=FORM_SECRET",
                }
            ],
            "frames": [
                {
                    "name": "checkout",
                    "title": "Checkout",
                    "src": "https://frames.test/embed?token=FRAME_SECRET",
                }
            ],
            "shadow_root_count": 2,
        }
    )

    assert model.page_id == "page-1"
    assert model.document_generation == 7
    assert model.url == "https://example.test/a/path"
    assert model.title == "Account [redacted]"
    assert model.controls[0].name == "Email [redacted]"
    assert model.headings[0].text == "Dashboard [redacted]"
    assert model.forms[0].action == "https://example.test/save"
    assert model.frames[0].src == "https://frames.test/embed"
    assert model.shadow_root_count == 2

    lowered = script.casefold()
    assert "document.cookie" not in lowered
    assert "localstorage" not in lowered
    assert "sessionstorage" not in lowered
    assert ".value" not in lowered


def test_site_model_does_not_expose_input_values_and_preserves_semantic_metadata() -> None:
    model, _ = _capture(
        {
            "url": "https://example.test/form",
            "title": "Form",
            "ready_state": "interactive",
            "controls": [
                {
                    "tag": "textarea",
                    "role": "textbox",
                    "name": "Operational goal",
                    "enabled": True,
                    "visible": True,
                    "contenteditable": False,
                    "value": "MUST_NOT_ESCAPE",
                },
                {
                    "tag": "div",
                    "role": "textbox",
                    "name": "Notes",
                    "enabled": True,
                    "visible": True,
                    "contenteditable": True,
                },
            ],
            "headings": [],
            "forms": [],
            "frames": [],
            "shadow_root_count": 1,
        }
    )

    assert [control.name for control in model.controls] == ["Operational goal", "Notes"]
    assert model.controls[1].contenteditable is True
    assert "MUST_NOT_ESCAPE" not in repr(model)


def test_site_model_rejects_oversized_or_malformed_payloads() -> None:
    payload = {
        "url": "https://example.test/",
        "title": "Example",
        "ready_state": "complete",
        "controls": [
            {
                "tag": "button",
                "role": "button",
                "name": f"Action {index}",
                "enabled": True,
                "visible": True,
                "contenteditable": False,
            }
            for index in range(101)
        ],
        "headings": [],
        "forms": [],
        "frames": [],
        "shadow_root_count": 0,
    }
    with pytest.raises(ValueError, match="exceeded bounded size"):
        _capture(payload)

    malformed = dict(payload, controls=[], shadow_root_count=True)
    with pytest.raises(ValueError, match="shadow_root_count"):
        _capture(malformed)

    malformed = dict(payload, controls=[], shadow_scan_truncated="yes")
    with pytest.raises(TypeError, match="shadow_scan_truncated"):
        _capture(malformed)


def test_site_model_fail_closes_unknown_urls_and_form_methods() -> None:
    model, _ = _capture(
        {
            "url": "javascript:alert(1)",
            "title": "Example",
            "ready_state": "mystery",
            "controls": [],
            "headings": [],
            "forms": [{"name": "Odd", "method": "trace", "action": "file:///tmp/x"}],
            "frames": [{"name": "x", "title": "X", "src": "data:text/html,secret"}],
            "shadow_root_count": 0,
        }
    )
    assert model.url == ""
    assert model.ready_state == "unknown"
    assert model.forms[0].method == "unknown"
    assert model.forms[0].action == ""
    assert model.frames[0].src == ""
