"""ASGI edge for the existing Nika-owned Web command/application boundary.

This is deliberately an adapter, not an identity service or a cloud scheduler.
An independently authenticated server middleware must populate scope["state"]
with an exact WebPrincipal under "nika_principal"; request headers cannot do so.
Only non-cookie, same-origin HTTPS JSON POSTs are admitted. A reverse proxy must
set a *trusted* ASGI scheme; forwarded browser headers are not consulted.
"""
from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from ipaddress import IPv6Address
from typing import Any
from urllib.parse import urlsplit

from nika_core.web_api.contracts import WebPrincipal
from nika_core.web_api.http_transport import (
    _MAX_HTTP_BODY_BYTES,
    HttpCommandAdapter,
    HttpCommandResponse,
)

Receive = Callable[[], Awaitable[dict[str, object]]]
Send = Callable[[dict[str, object]], Awaitable[None]]
_MAX_RECEIVE_EVENTS = 1024
_MAX_RECEIVE_SECONDS = 15.0  # Entire request body, not a fresh timeout per chunk.
_MAX_HEADER_FIELDS = 64
_MAX_HEADER_BYTES = 16 * 1024
_HTTP_TOKEN = frozenset(
    b"!#$%&'*+-.^_`|~0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
)

_UNIQUE_SECURITY_HEADERS = frozenset({
    b"authorization", b"content-length", b"content-type", b"cookie",
    b"host", b"origin", b"transfer-encoding",
})

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



def _observe_abandoned_receive(future: asyncio.Future[Any]) -> None:
    """Drain a late receive failure; it can never dispatch the command.

    The ASGI host owns network receive cleanup. We only cancel our temporary
    awaitable and consume its eventual exception without logging request data.
    """
    if not future.cancelled():
        try:
            future.exception()
        except Exception:
            pass


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
        # Reject explicitly malformed server protocol metadata before body I/O.
        # A synthetic/incorrect HTTP version must not evade the HTTP/1.1+
        # authority requirement or advertise an unsupported framing scheme.
        http_version = scope.get("http_version")
        if http_version is not None and (
            type(http_version) is not str
            or http_version not in ("1.0", "1.1", "2", "3")
        ):
            return self._error(400, "invalid_http_version")
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
            if key in _UNIQUE_SECURITY_HEADERS:
                if key in selected:
                    return self._error(400, "duplicate_security_header")
                selected[key] = value
        if b"content-length" in selected and b"transfer-encoding" in selected:
            return self._error(400, "conflicting_request_framing")
        # ASGI delivers decoded request bodies. Admit at most one canonical
        # HTTP/1.1 chunked transfer coding; ambiguous/unsupported codings and
        # absent-version/HTTP/1.0/HTTP/2/HTTP/3 transfer coding fails closed.
        # Never reinterpret the body or add a second HTTP framing parser.
        transfer_encoding = selected.get(b"transfer-encoding")
        if transfer_encoding is not None and (
            transfer_encoding != b"chunked"
            or http_version != "1.1"
        ):
            return self._error(400, "invalid_transfer_encoding")
        declared_length: int | None = None
        if b"content-length" in selected:
            encoded = selected[b"content-length"]
            if (
                not encoded
                or len(encoded) > 10
                or not all(48 <= digit <= 57 for digit in encoded)
            ):
                return self._error(400, "invalid_content_length")
            declared_length = int(encoded)
            if declared_length > _MAX_HTTP_BODY_BYTES:
                return self._error(413, "payload_too_large")
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
        # Host is browser-controlled input, never evidence of an authenticated
        # tenant. Pin supplied authorities to the configured trusted origin
        # inventory, including exact port and IPv6 bracket syntax. Without this
        # check an allowed Origin and a forged Host could reach the same Core
        # command handler through host-based middleware or reverse proxies.
        host_bytes = selected.get(b"host")
        # HTTP/1.1+ requires an authority. ASGI servers supply http_version;
        # do not let an omitted Host bypass the configured origin inventory.
        # Retain HTTP/1.0 compatibility where Host is optional.
        if http_version in ("1.1", "2", "3") and host_bytes is None:
            return self._error(403, "host_required")
        if host_bytes is not None:
            try:
                host = host_bytes.decode("ascii")
            except UnicodeDecodeError:
                return self._error(403, "host_forbidden")
            host_origin = f"https://{host}"
            if host_origin not in self._allowed_origins:
                return self._error(403, "host_forbidden")
            if origin_bytes is not None and origin != host_origin:
                return self._error(403, "host_forbidden")

        state = scope.get("state")
        principal = state.get("nika_principal") if type(state) is dict else None
        if type(principal) is not WebPrincipal:
            return self._error(401, "authentication_required")
        # The ASGI receive loop below awaits untrusted network input. Snapshot
        # and revalidate server-established identity *before* the first await,
        # so later mutation of the middleware's scope/principal cannot switch
        # tenant or workspace between admission and Core dispatch.
        try:
            trusted_principal = WebPrincipal(
                tenant_id=principal.tenant_id,
                user_id=principal.user_id,
                workspace_id=principal.workspace_id,
                session_id=principal.session_id,
            )
        except ValueError:
            return self._error(401, "authentication_required")
        content_type_bytes = selected.get(b"content-type", b"")
        try:
            content_type = content_type_bytes.decode("ascii")
        except UnicodeDecodeError:
            return self._error(415, "unsupported_media_type")

        chunks: list[bytes] = []
        length = 0
        # One monotonic, request-wide deadline prevents fragmented slow-loris
        # uploads from holding a Core execution slot indefinitely. This is
        # transport admission only, not a new task scheduler/retry mechanism.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _MAX_RECEIVE_SECONDS
        for _ in range(_MAX_RECEIVE_EVENTS):
            remaining = deadline - loop.time()
            if remaining <= 0:
                return self._error(408, "request_receive_timeout")
            try:
                # Shield the underlying receive so timeout/cancellation cannot
                # wait indefinitely for a non-cooperative ASGI receiver to
                # acknowledge cancellation. The late result is discarded.
                receive_future = asyncio.ensure_future(receive())
                try:
                    event = await asyncio.wait_for(
                        asyncio.shield(receive_future), timeout=remaining
                    )
                finally:
                    if not receive_future.done():
                        receive_future.cancel()
                    receive_future.add_done_callback(_observe_abandoned_receive)
            except TimeoutError:
                # No Core command has been dispatched. Do not retry or leak
                # any fragment, session value, or exception text.
                return self._error(408, "request_receive_timeout")
            except Exception:  # noqa: BLE001 - fail closed on pre-effect ASGI I/O faults
                # Receive failed before Core dispatch: no write outcome is unknown.
                # Do not leak exception text (possibly including request/secret data).
                # asyncio.CancelledError remains uncaught so host cancellation works.
                return self._error(500, "request_receive_failed")
            # asyncio.wait_for can return *after* its timeout when an ASGI
            # receive coroutine suppresses cancellation and yields a late
            # complete body. Recheck the absolute deadline before effects;
            # a timed-out upload cannot be resurrected by a hostile receiver.
            if loop.time() >= deadline:
                return self._error(408, "request_receive_timeout")
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
                if declared_length is not None and length != declared_length:
                    return self._error(400, "invalid_content_length")
                try:
                    return self._adapter.handle(
                        principal=trusted_principal,
                        method="POST",
                        content_type=content_type,
                        body=b"".join(chunks),
                    )
                except Exception:  # noqa: BLE001 - untrusted ASGI failures must be sanitized
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
