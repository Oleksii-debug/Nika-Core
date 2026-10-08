"""Plan 6 section 1: ASGI edge safety without real accounts or cloud credentials."""
from __future__ import annotations

import asyncio
import json

import pytest

from nika_core.web_api import WebApplicationBoundary, WebCommandResult, WebPrincipal
from nika_core.web_api.asgi import ASGICommandApplication
from nika_core.web_api.http_transport import HttpCommandAdapter


class _Allow:
    def allows(self, principal, command) -> bool:
        return principal.tenant_id == "tenant-a" and command.action_id == "task.create"


class _Handler:
    def __init__(self) -> None:
        self.calls = 0

    def handle(self, principal, command):
        self.calls += 1
        return WebCommandResult.create(
            request_id=command.request_id,
            status="completed",
            code="ok",
            message="Готово.",
            data={"workspace": principal.workspace_id},
        )


def _principal(tenant: str = "tenant-a") -> WebPrincipal:
    return WebPrincipal(
        tenant_id=tenant,
        user_id="user-a",
        workspace_id="workspace-a",
        session_id="session-a",
    )


def _body() -> bytes:
    return json.dumps({
        "request_id": "test-request",
        "action_id": "task.create",
        "payload": {"command": "inspect"},
    }).encode("utf-8")


def _app() -> tuple[ASGICommandApplication, _Handler]:
    handler = _Handler()
    boundary = WebApplicationBoundary(authorization=_Allow(), handler=handler)
    adapter = HttpCommandAdapter(boundary)
    return ASGICommandApplication(
        adapter, allowed_origins=frozenset({"https://nika.example"})
    ), handler


def _scope(
    *,
    principal: WebPrincipal | None = None,
    origin: bytes = b"https://nika.example",
    scheme: str = "https",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> dict[str, object]:
    return {
        "type": "http",
        "scheme": scheme,
        "path": "/v1/commands",
        "query_string": b"",
        "method": "POST",
        "headers": headers if headers is not None else [
            (b"origin", origin),
            (b"content-type", b"application/json"),
        ],
        "state": {"nika_principal": principal} if principal else {},
    }


def _call(
    app: ASGICommandApplication,
    scope: dict[str, object],
    events: list[dict[str, object]] | None = None,
) -> tuple[dict[str, object], ...]:
    source = list(
        events if events is not None else [
            {"type": "http.request", "body": _body(), "more_body": False}
        ]
    )
    output: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return source.pop(0)

    async def send(value: dict[str, object]) -> None:
        output.append(value)

    asyncio.run(app(scope, receive, send))
    return tuple(output)


def _status(output: tuple[dict[str, object], ...]) -> int:
    return int(output[0]["status"])


def _payload(output: tuple[dict[str, object], ...]) -> dict[str, object]:
    return json.loads(output[1]["body"].decode("utf-8"))  # type: ignore[union-attr]


def test_authorized_request_reuses_existing_boundary_and_response_headers() -> None:
    app, handler = _app()
    output = _call(app, _scope(principal=_principal()))
    assert _status(output) == 200
    assert _payload(output)["data"] == {"workspace": "workspace-a"}
    headers = dict(output[0]["headers"])  # type: ignore[arg-type]
    assert headers[b"cache-control"] == b"no-store"
    assert headers[b"x-content-type-options"] == b"nosniff"
    assert handler.calls == 1


@pytest.mark.parametrize(
    ("scope", "expected"),
    [
        (_scope(), 401),
        (_scope(principal=_principal("tenant-b")), 403),
        (_scope(principal=_principal(), scheme="http"), 403),
        (_scope(principal=_principal(), origin=b"https://attacker.example"), 403),
        (_scope(principal=_principal(), headers=[
            (b"content-type", b"application/json"),
            (b"cookie", b"session=forged"),
        ]), 403),
        (_scope(principal=_principal(), headers=[
            (b"content-type", b"application/json"),
            (b"Content-Type", b"application/json"),
        ]), 400),
    ],
)
def test_untrusted_or_cross_tenant_requests_fail_closed_without_effect(
    scope: dict[str, object], expected: int
) -> None:
    app, handler = _app()
    output = _call(app, scope)
    assert _status(output) == expected
    assert handler.calls == 0
    assert b"forged" not in output[1]["body"]


def test_oversize_chunk_is_bounded_before_canonical_handler() -> None:
    app, handler = _app()
    output = _call(
        app,
        _scope(principal=_principal()),
        [{"type": "http.request", "body": b"x" * (256 * 1024 + 1)}],
    )
    assert _status(output) == 413
    assert handler.calls == 0


def test_chunked_request_uses_same_strict_existing_http_contract() -> None:
    app, handler = _app()
    body = _body()
    output = _call(
        app, _scope(principal=_principal()), [
            {"type": "http.request", "body": body[:10], "more_body": True},
            {"type": "http.request", "body": body[10:], "more_body": False},
        ]
    )
    assert _status(output) == 200
    assert handler.calls == 1


def test_client_disconnect_does_not_dispatch_effect_or_emit_response() -> None:
    app, handler = _app()
    output = _call(
        app, _scope(principal=_principal()), [{"type": "http.disconnect"}]
    )
    assert output == ()
    assert handler.calls == 0


def test_malformed_stream_fails_without_raw_detail_or_effect() -> None:
    app, handler = _app()
    output = _call(
        app, _scope(principal=_principal()),
        [{"type": "http.request", "body": "not bytes"}],
    )
    assert _status(output) == 400
    assert handler.calls == 0
    assert _payload(output)["code"] == "invalid_request_stream"


def test_configuration_rejects_non_https_and_wildcard_origins() -> None:
    boundary = WebApplicationBoundary(authorization=_Allow(), handler=_Handler())
    adapter = HttpCommandAdapter(boundary)
    for origin in ("http://nika.example", "*", "https://nika.example/"):
        with pytest.raises(ValueError, match="origins"):
            ASGICommandApplication(adapter, allowed_origins=frozenset({origin}))


@pytest.mark.parametrize(
    "bad_origin",
    [
        "https://user@nika.example",
        "https://nika.example/path",
        "https://nika.example?debug=1",
        "https://nika.example#fragment",
        "https://nika.example\\\@evil.example",
        "https://nika.example:99999",
        "https://nika.example:abc",
        "https://nika.example:0",
        "https://nika.example:0443",
        "https://NIKA.example",
        "https://nika.example.",
        "https://nika..example",
        "https://%6eika.example",
        "https://nika.example\\n",
        "https://ніка.example",
        "https://[::1",
    ],
)
def test_config_rejects_malformed_or_ambiguous_origins(bad_origin: str) -> None:
    boundary = WebApplicationBoundary(authorization=_Allow(), handler=_Handler())
    with pytest.raises(ValueError, match="origins"):
        ASGICommandApplication(
            HttpCommandAdapter(boundary), allowed_origins=frozenset({bad_origin})
        )


@pytest.mark.parametrize("good_origin", [
    "https://nika.example",
    "https://localhost:8443",
    "https://127.0.0.1:8443",
    "https://[::1]:8443",
])
def test_config_accepts_canonical_https_origin(good_origin: str) -> None:
    boundary = WebApplicationBoundary(authorization=_Allow(), handler=_Handler())
    app = ASGICommandApplication(
        HttpCommandAdapter(boundary), allowed_origins=frozenset({good_origin})
    )
    scope = _scope(principal=_principal(), origin=good_origin.encode("ascii"))
    assert _status(_call(app, scope)) == 200


def test_config_rejection_prevents_untrusted_host_from_admission() -> None:
    app, handler = _app()
    output = _call(app, _scope(
        principal=_principal(),
        origin=b"https://nika.example.evil.example",
    ))
    assert _status(output) == 403
    assert handler.calls == 0


@pytest.mark.parametrize("headers, expected", [
    ([(b"X-Test", b"x")] * 65, 431),
    ([(b"x-large", b"x" * (16 * 1024 + 1))], 431),
    ([(b"bad\nname", b"hello")], 400),
    ([(b"x-safe", b"bad\x00value")], 400),
    ([(b"x-safe", b"bad\r\nset-cookie: forged")], 400),
    ([(b"", b"empty")], 400),
])
def test_oversized_or_malformed_headers_fail_before_effect(
    headers: list[tuple[bytes, bytes]], expected: int
) -> None:
    app, handler = _app()
    headers += [
        (b"origin", b"https://nika.example"),
        (b"content-type", b"application/json"),
    ]
    output = _call(app, _scope(principal=_principal(), headers=headers))
    assert _status(output) == expected
    assert handler.calls == 0


def test_canonical_headers_accept_additional_bounded_field() -> None:
    app, handler = _app()
    output = _call(app, _scope(principal=_principal(), headers=[
        (b"origin", b"https://nika.example"),
        (b"content-type", b"application/json"),
        (b"x-trace-id", b"opaque-123"),
    ]))
    assert _status(output) == 200
    assert handler.calls == 1
