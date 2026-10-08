"""Read-only accessible Web shell over the existing Web/Cloud command ingress.

Only static public assets are served here; account, tenant, entitlement and
Core effect authority remain entirely with the trusted server/ASGI boundary.
"""
from __future__ import annotations

from functools import lru_cache
from importlib.resources import files
from typing import Any

from nika_core.web_api.asgi import ASGICommandApplication, Receive, Send

_ASSETS = {
    "/app/": ("index.html", b"text/html; charset=utf-8"),
    "/app/client.js": ("client.js", b"text/javascript; charset=utf-8"),
    "/app/styles.css": ("styles.css", b"text/css; charset=utf-8"),
}
_CSP = (
    b"default-src 'none'; script-src 'self'; style-src 'self'; "
    b"connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


@lru_cache(maxsize=3)
def _asset_bytes(filename: str) -> bytes:
    if filename not in {"index.html", "client.js", "styles.css"}:
        raise ValueError("invalid static asset")
    return files("nika_core.web_api.client").joinpath(filename).read_bytes()


class AccessibleWebClientApplication:
    """Static route adapter, delegating all commands to canonical ASGI ingress."""

    def __init__(self, commands: ASGICommandApplication) -> None:
        if type(commands) is not ASGICommandApplication:
            raise ValueError("use the existing Nika ASGI command adapter")
        self._commands = commands

    async def __call__(self, scope: dict[str, Any], receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            raise RuntimeError("Web client only supports HTTP")
        if scope.get("path") == "/v1/commands":
            await self._commands(scope, receive, send)
            return

        path = scope.get("path")
        if scope.get("scheme") != "https":
            status, body, mime = 403, b"", b"text/plain; charset=utf-8"
        elif type(path) is not str or path not in _ASSETS or scope.get("query_string", b""):
            status, body, mime = 404, b"", b"text/plain; charset=utf-8"
        elif scope.get("method") != "GET":
            status, body, mime = 405, b"", b"text/plain; charset=utf-8"
        elif not self._trusted_static_host(scope.get("headers")):
            status, body, mime = 403, b"", b"text/plain; charset=utf-8"
        else:
            filename, mime = _ASSETS[path]
            try:
                body = _asset_bytes(filename)
            except OSError:
                status, body, mime = 500, b"", b"text/plain; charset=utf-8"
            else:
                status = 200

        headers = [
            (b"content-type", mime),
            (b"cache-control", b"no-store"),
            (b"x-content-type-options", b"nosniff"),
            (b"referrer-policy", b"no-referrer"),
            (b"content-security-policy", _CSP),
        ]
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body, "more_body": False})

    def _trusted_static_host(self, headers: object) -> bool:
        if type(headers) not in {list, tuple} or len(headers) > 64:
            return False
        hosts: list[bytes] = []
        for pair in headers:
            if type(pair) not in {list, tuple} or len(pair) != 2:
                return False
            name, value = pair
            if type(name) is not bytes or type(value) is not bytes:
                return False
            if name.lower() == b"host":
                hosts.append(value)
        if len(hosts) != 1:
            return False
        try:
            authority = hosts[0].decode("ascii")
        except UnicodeDecodeError:
            return False
        # The canonical command adapter already validates this exact inventory.
        return f"https://{authority}" in self._commands._allowed_origins
