"""ASGI edge for the existing Nika-owned Web command/application boundary.

This is deliberately an adapter, not an identity service or a cloud scheduler.
An independently authenticated server middleware must populate scope["state"]
with an exact WebPrincipal under "nika_principal"; request headers cannot do so.
Only non-cookie, same-origin HTTPS JSON POSTs are admitted. A reverse proxy must
set a *trusted* ASGI scheme; forwarded browser headers are not consulted.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from ipaddress import IPv6Address
import re
from typing import Any
from urllib.parse import urlsplit

from nika_core.web_api.contracts import WebPrincipal
from nika_core.web_api.http_transport import (
    HttpCommandAdapter,
    HttpCommandResponse,
    _MAX_HTTP_BODY_BYTES,
)

Receive = Callable[[], Awaitable[dict[str, object]]]
Send = Callable[[dict[str, object]], Awaitable[None]]
_MAX_RECEIVE_EVENTS = 1024
_MAX_HEADER_FIELDS = 64
_MAX_HEADER_BYTES = 16 * 1024
_HTTP_TOKEN = frozenset(b"!#$%&'*+-.^_`|~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz")


_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


def _is_canonical_https_origin(value: object) -> bool:
    """Reject ambiguous/invalid Origin allowlist entries before any request exists.

    Browsers serialize Origin as scheme://host[:port], never a URL with
    userinfo, path, query or fragment. Accept ASCII DNS/IPv4 and bracketed
    IPv6 only, with normalized lowercase host and a valid optional port.
    """
    if type(value) is not str or len(value) > 255:
        return False
    if not value.startswith("https://") or any(
        ord(char) < 33 or ord(char) > 126 for char in value
    ):
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    host = parsed.hostname
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or host is None
        or "%" in host
        or port == 0
    ):
        return False
    if ":" in host:
        try:
            IPv6Address(host)
        except ValueError:
            return False
        authority = f"[{host}]"
    else:
        if (
            len(host) > 253
            or host.endswith(".")
            or not all(_DNS_LABEL.fullmatch(label) for label in host.split("."))
        ):
            return False
        authority = host
    if port is not None:
        authority += f":{port}"
    return value == f"https://{authority}"


class ASGICommandApplication:
    """Small, framework-neutral Web ingress; canonical Core remains the executor."""

    def __init__(
        self,
        adapter: HttpCommandAdapter,
        *,
        allowed_origins: frozenset[str],
    ) -> None:
        if type(adapter) is not HttpCommandAdapter:
            raise ValueError("adapter must be the existing HTTP command adapter")
        if type(allowed_origins) is not frozenset or not allowed_origins:
            raise ValueError("configure explicit trusted HTTPS Web origins")
        if any(not _is_canonical_https_origin(origin) for origin in allowed_origins):
            raise ValueError("allowed origins must be exact HTTPS origins")
        self._adapter = adapter
        self._allowed_origins = allowed_origins

    async def __call__(
        self,
        scope: Mapping[str, Any],
        receive: Receive,
        send: Send,
    ) -> None:
        if scope.get("type") != "http":
            raise RuntimeError("ASGI command adapter handles HTTP scopes only")

        response: HttpCommandResponse
        if scope.get("scheme") != "https":
            response = self._error(403, "https_required")
        elif scope.get("path") != "/v1/commands" or scope.get("query_string", b""):
            response = self._error(404, "not_found")
        elif scope.get("method") != "POST":
            response = self._error(405, "method_not_allowed")
        else:
            response = await self._handle_post(scope, receive)
            if response is None:
                return  # Client disconnected; do not begin any application effect.

        await send({
            "type": "http.response.start",
            "status": response.status_code,
            "headers": [
                (name.encode("ascii"), value.encode("ascii"))
                for name, value in response.headers
            ],
        })
        await send({"type": "http.response.body", "body": response.body, "more_body": False})

    async def _handle_post(
        self,
        scope: Mapping[str, Any],
        receive: Receive,
    ) -> HttpCommandResponse | None:
        headers = scope.get("headers")
        if type(headers) not in {tuple, list}:
            return self._error(400, "invalid_headers")
        if len(headers) > _MAX_HEADER_FIELDS:
            return self._error(431, "request_headers_too_large")
        selected: dict[bytes, bytes] = {}
        header_bytes = 0
        for item in headers:
            if type(item) not in {tuple, list} or len(item) != 2:
                return self._error(400, "invalid_headers")
            name, value = item
            if type(name) is not bytes or type(value) is not bytes:
                return self._error(400, "invalid_headers")
            header_bytes += len(name) + len(value)
            if header_bytes > _MAX_HEADER_BYTES:
                return self._error(431, "request_headers_too_large")
            if (
                not name
                or any(char not in _HTTP_TOKEN for char in name)
                or any(char in value for char in (0, 10, 13))
            ):
                return self._error(400, "invalid_headers")
            key = name.lower()
            if key in {b"content-type", b"origin", b"cookie"}:
                if key in selected:
                    return self._error(400, "duplicate_security_header")
                selected[key] = value
        if b"cookie" in selected:
            # Until server-owned CSRF/session semantics exist, cookie auth is not admitted.
            return self._error(403, "cookie_auth_unavailable")
        origin_bytes = selected.get(b"origin")
        if origin_bytes is not None:
            try:
                origin = origin_bytes.decode("ascii")
            except UnicodeDecodeError:
                return self._error(403, "origin_forbidden")
            if origin not in self._allowed_origins:
                return self._error(403, "origin_forbidden")
        state = scope.get("state")
        principal = state.get("nika_principal") if type(state) is dict else None
        if type(principal) is not WebPrincipal:
            return self._error(401, "authentication_required")
        content_type_bytes = selected.get(b"content-type", b"")
        try:
            content_type = content_type_bytes.decode("ascii")
        except UnicodeDecodeError:
            return self._error(415, "unsupported_media_type")

        chunks: list[bytes] = []
        length = 0
        for _ in range(_MAX_RECEIVE_EVENTS):
            event = await receive()
            if type(event) is not dict:
                return self._error(400, "invalid_request_stream")
            kind = event.get("type")
            if kind == "http.disconnect":
                return None
            if kind != "http.request":
                return self._error(400, "invalid_request_stream")
            chunk = event.get("body", b"")
            more = event.get("more_body", False)
            if type(chunk) is not bytes or type(more) is not bool:
                return self._error(400, "invalid_request_stream")
            length += len(chunk)
            if length > _MAX_HTTP_BODY_BYTES:
                return self._error(413, "payload_too_large")
            chunks.append(chunk)
            if not more:
                try:
                    return self._adapter.handle(
                        principal=principal,
                        method="POST",
                        content_type=content_type,
                        body=b"".join(chunks),
                    )
                except Exception:
                    # Framework/transport faults never disclose raw errors or credentials.
                    return self._error(500, "internal_error")
        return self._error(413, "request_stream_too_long")

    @staticmethod
    def _error(status: int, code: str) -> HttpCommandResponse:
        return HttpCommandAdapter._error(
            status,
            code,
            "Web request cannot be completed.",
        )
