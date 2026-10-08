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


@pytest.mark.parametrize("name", [
    b"host", b"authorization", b"content-length", b"transfer-encoding",
])
def test_duplicate_server_authority_or_framing_header_rejected_before_effect(
    name: bytes,
) -> None:
    app, handler = _app()
    headers = [
        (b"origin", b"https://nika.example"),
        (b"content-type", b"application/json"),
        (name, b"a"),
        (name.upper(), b"b"),
    ]
    output = _call(app, _scope(principal=_principal(), headers=headers))
    assert _status(output) == 400
    assert _payload(output)["code"] == "duplicate_security_header"
    assert handler.calls == 0


@pytest.mark.parametrize("framing, expected_status, expected_code", [
    ([(b"content-length", b"-1")], 400, "invalid_content_length"),
    ([(b"content-length", b"1.0")], 400, "invalid_content_length"),
    ([(b"content-length", b"1 2")], 400, "invalid_content_length"),
    ([(b"content-length", b"99999999999999999999")],
     400, "invalid_content_length"),
    ([(b"content-length", b"262145")], 413, "payload_too_large"),
    ([(b"content-length", b"1"), (b"transfer-encoding", b"chunked")],
     400, "conflicting_request_framing"),
])
def test_framing_attack_cannot_dispatch_core_effect(
    framing: list[tuple[bytes, bytes]], expected_status: int, expected_code: str,
) -> None:
    app, handler = _app()
    headers = [(b"content-type", b"application/json"), *framing]
    output = _call(app, _scope(principal=_principal(), headers=headers))
    assert _status(output) == expected_status
    assert _payload(output)["code"] == expected_code
    assert handler.calls == 0


def test_declared_content_length_mismatch_has_no_effect() -> None:
    app, handler = _app()
    output = _call(app, _scope(principal=_principal(), headers=[
        (b"content-type", b"application/json"),
        (b"content-length", b"1"),
    ]))
    assert _status(output) == 400
    assert _payload(output)["code"] == "invalid_content_length"
    assert handler.calls == 0


def test_matching_content_length_and_fragmented_body_keep_core_boundary() -> None:
    app, handler = _app()
    body = _body()
    output = _call(app, _scope(principal=_principal(), headers=[
        (b"origin", b"https://nika.example"),
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]), events=[
        {"type": "http.request", "body": body[:9], "more_body": True},
        {"type": "http.request", "body": body[9:], "more_body": False},
    ])
    assert _status(output) == 200
    assert handler.calls == 1


@pytest.mark.parametrize(
    ("host", "origin", "expected_code"),
    [
        (b"other.example", b"https://nika.example", "host_forbidden"),
        (b"nika.example.evil.example", b"https://nika.example", "host_forbidden"),
        (b"NIKA.example", b"https://nika.example", "host_forbidden"),
        (b"nika.example:443", b"https://nika.example", "host_forbidden"),
        (b"user@nika.example", b"https://nika.example", "host_forbidden"),
        (b"", b"https://nika.example", "host_forbidden"),
        (b"\xff", b"https://nika.example", "host_forbidden"),
        (b"other.example", b"", "host_forbidden"),
    ],
)
def test_supplied_host_cannot_redirect_canonical_web_authority(
    host: bytes, origin: bytes, expected_code: str,
) -> None:
    app, handler = _app()
    headers = [(b"host", host), (b"content-type", b"application/json")]
    if origin:
        headers.append((b"origin", origin))
    output = _call(app, _scope(principal=_principal(), headers=headers))
    assert _status(output) == 403
    assert _payload(output)["code"] == expected_code
    assert handler.calls == 0


def test_matching_host_origin_and_explicit_nonbrowser_host_remain_valid() -> None:
    app, handler = _app()
    for with_origin in (True, False):
        headers = [
            (b"host", b"nika.example"),
            (b"content-type", b"application/json"),
        ]
        if with_origin:
            headers.append((b"origin", b"https://nika.example"))
        result = _call(app, _scope(principal=_principal(), headers=headers))
        assert _status(result) == 200
    assert handler.calls == 2


def test_header_case_duplicate_host_and_injected_host_never_dispatch() -> None:
    app, handler = _app()
    output = _call(app, _scope(principal=_principal(), headers=[
        (b"Host", b"nika.example"),
        (b"host", b"nika.example"),
        (b"origin", b"https://nika.example"),
        (b"content-type", b"application/json"),
    ]))
    assert _status(output) == 400
    assert _payload(output)["code"] == "duplicate_security_header"
    poisoned = _call(app, _scope(principal=_principal(), headers=[
        (b"host", b"nika.example\r\nX-Authority: poisoned"),
        (b"content-type", b"application/json"),
    ]))
    assert _status(poisoned) == 400
    assert _payload(poisoned)["code"] == "invalid_headers"
    assert handler.calls == 0


@pytest.mark.parametrize("http_version", ["1.1", "2", "3"])
def test_modern_http_without_host_is_rejected_before_receive_or_core_effect(
    http_version: str,
) -> None:
    app, handler = _app()
    scope = _scope(principal=_principal())
    scope["http_version"] = http_version
    output = _call(app, scope, events=[])
    assert _status(output) == 403
    assert _payload(output)["code"] == "host_required"
    assert handler.calls == 0


def test_http11_with_trusted_host_continues_to_core() -> None:
    app, handler = _app()
    scope = _scope(principal=_principal(), headers=[
        (b"host", b"nika.example"),
        (b"origin", b"https://nika.example"),
        (b"content-type", b"application/json"),
    ])
    scope["http_version"] = "1.1"
    output = _call(app, scope)
    assert _status(output) == 200
    assert handler.calls == 1


def test_host_rejection_precedes_receive_even_for_unbounded_body() -> None:
    app, handler = _app()
    output = _call(app, _scope(principal=_principal(), headers=[
        (b"host", b"unauthorized.example"),
        (b"origin", b"https://nika.example"),
        (b"content-type", b"application/json"),
    ]), events=[])
    assert _status(output) == 403
    assert _payload(output)["code"] == "host_forbidden"
    assert handler.calls == 0


@pytest.mark.parametrize("origin", [
    "https://localhost:8443",
    "https://127.0.0.1:8443",
    "https://[::1]:8443",
])
def test_configured_ipv4_ipv6_port_hosts_reach_existing_core_boundary(
    origin: str,
) -> None:
    handler = _Handler()
    boundary = WebApplicationBoundary(authorization=_Allow(), handler=handler)
    app = ASGICommandApplication(
        HttpCommandAdapter(boundary), allowed_origins=frozenset({origin}),
    )
    host = origin.removeprefix("https://").encode("ascii")
    output = _call(app, _scope(principal=_principal(), headers=[
        (b"host", host),
        (b"origin", origin.encode("ascii")),
        (b"content-type", b"application/json"),
    ]))
    assert _status(output) == 200
    assert handler.calls == 1


def test_asgi_receive_cannot_swap_server_tenant_or_workspace_in_flight() -> None:
    app, handler = _app()
    principal = _principal()
    scope = _scope(principal=principal)
    output: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        object.__setattr__(principal, "workspace_id", "other-workspace")
        object.__setattr__(principal, "tenant_id", "other-tenant")
        return {"type": "http.request", "body": _body(), "more_body": False}

    async def send(message: dict[str, object]) -> None:
        output.append(message)

    asyncio.run(app(scope, receive, send))
    assert _status(tuple(output)) == 200
    assert _payload(tuple(output))["data"] == {"workspace": "workspace-a"}
    assert handler.calls == 1


def test_mutated_invalid_server_principal_is_rejected_before_network_read() -> None:
    app, handler = _app()
    principal = _principal()
    object.__setattr__(principal, "workspace_id", "bad\nworkspace")
    result = _call(app, _scope(principal=principal), events=[])
    assert _status(result) == 401
    assert _payload(result)["code"] == "authentication_required"
    assert handler.calls == 0


@pytest.mark.parametrize("invalid_version", [
    "", "1", "1.01", "2.0", "HTTP/1.1", 11, True, [], {},
])
def test_explicit_invalid_asgi_http_version_fails_before_receive(
    invalid_version: object,
) -> None:
    app, handler = _app()
    scope = _scope(principal=_principal(), headers=[
        (b"host", b"nika.example"),
        (b"content-type", b"application/json"),
    ])
    scope["http_version"] = invalid_version
    output = _call(app, scope, events=[])
    assert _status(output) == 400
    assert _payload(output)["code"] == "invalid_http_version"
    assert handler.calls == 0


@pytest.mark.parametrize(("version", "coding"), [
    ("1.0", b"chunked"),
    ("1.1", b"gzip"),
    ("1.1", b"CHUNKED"),
    ("1.1", b"chunked, gzip"),
    ("1.1", b"chunked\\r\\n"),
    ("2", b"chunked"),
    ("3", b"chunked"),
])
def test_ambiguous_or_protocol_forbidden_transfer_coding_has_no_core_effect(
    version: str, coding: bytes,
) -> None:
    app, handler = _app()
    scope = _scope(principal=_principal(), headers=[
        (b"host", b"nika.example"),
        (b"content-type", b"application/json"),
        (b"transfer-encoding", coding),
    ])
    scope["http_version"] = version
    output = _call(app, scope, events=[])
    assert _status(output) == 400
    assert _payload(output)["code"] == "invalid_transfer_encoding"
    assert handler.calls == 0


def test_canonical_http11_chunked_asgi_body_dispatches_to_existing_core() -> None:
    app, handler = _app()
    scope = _scope(principal=_principal(), headers=[
        (b"host", b"nika.example"),
        (b"origin", b"https://nika.example"),
        (b"content-type", b"application/json"),
        (b"transfer-encoding", b"chunked"),
    ])
    scope["http_version"] = "1.1"
    body = _body()
    output = _call(app, scope, events=[
        {"type": "http.request", "body": body[:7], "more_body": True},
        {"type": "http.request", "body": body[7:], "more_body": False},
    ])
    assert _status(output) == 200
    assert handler.calls == 1


def test_http10_without_transfer_coding_keeps_legacy_optional_host() -> None:
    app, handler = _app()
    scope = _scope(principal=_principal())
    scope["http_version"] = "1.0"
    output = _call(app, scope)
    assert _status(output) == 200
    assert handler.calls == 1
