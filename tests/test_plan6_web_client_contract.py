"""Plan 6 §2 static browser adapter: semantics, routing and host fences."""
from __future__ import annotations

import asyncio
from html.parser import HTMLParser
from importlib.resources import files

import pytest

from nika_core.web_api import (
    ASGICommandApplication,
    AccessibleWebClientApplication,
    HttpCommandAdapter,
    WebApplicationBoundary,
)


class _DenyAll:
    def allows(self, _principal, _command) -> bool:
        return False


class _NoEffects:
    def __init__(self) -> None:
        self.calls = 0

    def handle(self, _principal, _command):
        self.calls += 1
        raise AssertionError("untrusted Web shell cannot invoke Core")


def _app() -> tuple[AccessibleWebClientApplication, _NoEffects]:
    handler = _NoEffects()
    commands = ASGICommandApplication(
        HttpCommandAdapter(WebApplicationBoundary(
            authorization=_DenyAll(), handler=handler,
        )),
        allowed_origins=frozenset({"https://nika.example"}),
    )
    return AccessibleWebClientApplication(commands), handler


def _request(app, **changes):
    scope = {
        "type": "http", "scheme": "https", "method": "GET",
        "path": "/app/", "query_string": b"",
        "headers": [(b"host", b"nika.example")],
    }
    scope.update(changes)
    messages = []

    async def receive():
        raise AssertionError("static Web page must not read a command body")

    async def send(message):
        messages.append(message)

    asyncio.run(app(scope, receive, send))
    return messages[0], messages[1]


class _SemanticInventory(HTMLParser):
    def __init__(self):
        super().__init__()
        self.elements = []
        self.attrs = []

    def handle_starttag(self, tag, attrs):
        self.elements.append(tag)
        self.attrs.append((tag, dict(attrs)))


def test_static_html_has_keyboard_and_screen_reader_semantics() -> None:
    document = (
        files("nika_core.web_api.client").joinpath("index.html").read_text("utf-8")
    )
    parsed = _SemanticInventory()
    parsed.feed(document)
    assert {"header", "main", "form", "h1", "h2", "label", "button", "footer"} <= set(
        parsed.elements
    )
    attributes = parsed.attrs
    assert any(tag == "a" and attrs.get("href") == "#main" for tag, attrs in attributes)
    assert any(tag == "label" and attrs.get("for") == "task-id" for tag, attrs in attributes)
    assert any(tag == "input" and attrs.get("id") == "task-id"
               and "required" in attrs for tag, attrs in attributes)
    assert any(attrs.get("role") == "status" and attrs.get("aria-live") == "polite"
               for _, attrs in attributes)
    assert any(tag == "script" and attrs.get("src") == "/app/client.js"
               for tag, attrs in attributes)


def test_browser_script_has_no_client_owned_tenant_or_auto_retry() -> None:
    script = files("nika_core.web_api.client").joinpath("client.js").read_text("utf-8")
    assert 'action_id: "task.inspect"' in script
    assert 'credentials: "omit"' in script
    assert 'cache: "no-store"' in script
    assert 'redirect: "error"' in script
    assert "textContent" in script
    assert "setTimeout" in script and "AbortController" in script
    assert "localStorage" not in script and "sessionStorage" not in script
    assert "innerHTML" not in script and "eval(" not in script


@pytest.mark.parametrize(
    ("path", "expected_mime"),
    [
        ("/app/", b"text/html; charset=utf-8"),
        ("/app/client.js", b"text/javascript; charset=utf-8"),
        ("/app/styles.css", b"text/css; charset=utf-8"),
    ],
)
def test_static_routes_are_secure_bounded_and_effect_free(path, expected_mime) -> None:
    app, handler = _app()
    start, body = _request(app, path=path)
    assert start["status"] == 200
    assert len(body["body"]) < 65536
    headers = dict(start["headers"])
    assert headers[b"content-type"] == expected_mime
    assert headers[b"cache-control"] == b"no-store"
    assert headers[b"x-content-type-options"] == b"nosniff"
    assert b"default-src 'none'" in headers[b"content-security-policy"]
    assert b"frame-ancestors 'none'" in headers[b"content-security-policy"]
    assert handler.calls == 0


@pytest.mark.parametrize(
    ("changes", "status"),
    [
        ({"scheme": "http"}, 403),
        ({"path": "/app/private"}, 404),
        ({"query_string": b"debug=1"}, 404),
        ({"method": "POST"}, 405),
        ({"headers": []}, 403),
        ({"headers": [(b"host", b"evil.example")]}, 403),
        ({"headers": [(b"host", b"nika.example"),
                      (b"host", b"nika.example")]}, 403),
    ],
)
def test_untrusted_static_access_fails_closed_without_core_effect(changes, status) -> None:
    app, handler = _app()
    start, body = _request(app, **changes)
    assert start["status"] == status
    assert body["body"] == b""
    assert handler.calls == 0


def test_commands_delegate_to_existing_server_ingress_without_browser_identity() -> None:
    app, handler = _app()
    start, body = _request(
        app,
        path="/v1/commands",
        method="POST",
        headers=[
            (b"host", b"nika.example"),
            (b"content-type", b"application/json"),
        ],
    )
    # Because no trusted server principal was established, the command
    # MUST NOT reach a policy or Core handler.
    assert start["status"] == 401
    assert b"authentication_required" in body["body"]
    assert handler.calls == 0


def test_rejects_unrelated_asgi_protocol() -> None:
    app, _ = _app()
    with pytest.raises(RuntimeError, match="HTTP"):
        _request(app, type="websocket")
