"""Plan 6 Section 1: fail-closed ASGI receive errors before canonical effects."""
from __future__ import annotations

import asyncio
import json

import pytest

from nika_core.web_api import WebApplicationBoundary, WebCommandResult, WebPrincipal
from nika_core.web_api.asgi import ASGICommandApplication
from nika_core.web_api.http_transport import HttpCommandAdapter


class _Allow:
    def allows(self, principal, command) -> bool:
        return principal.tenant_id == "tenant-a"


class _Effect:
    def __init__(self) -> None:
        self.calls = 0

    def handle(self, principal, command):
        self.calls += 1
        return WebCommandResult.create(
            request_id=command.request_id,
            status="completed",
            code="ok",
            message="Completed.",
        )


def _setup():
    handler = _Effect()
    boundary = WebApplicationBoundary(authorization=_Allow(), handler=handler)
    app = ASGICommandApplication(
        HttpCommandAdapter(boundary),
        allowed_origins=frozenset({"https://nika.example"}),
    )
    scope = {
        "type": "http",
        "scheme": "https",
        "path": "/v1/commands",
        "query_string": b"",
        "method": "POST",
        "headers": [
            (b"origin", b"https://nika.example"),
            (b"content-type", b"application/json"),
        ],
        "state": {"nika_principal": WebPrincipal(
            tenant_id="tenant-a",
            user_id="user-a",
            workspace_id="workspace-a",
            session_id="session-a",
        )},
    }
    body = json.dumps({
        "request_id": "receive-1",
        "action_id": "task.inspect",
        "payload": {},
    }).encode("utf-8")
    return app, handler, scope, body


def _invoke(app, scope, receive):
    output = []

    async def send(event):
        output.append(event)

    asyncio.run(app(scope, receive, send))
    return output


@pytest.mark.parametrize("after_fragment", [False, True])
@pytest.mark.parametrize("error", [OSError, ValueError, RuntimeError])
def test_receive_exception_is_sanitized_and_can_recover(
    after_fragment, error
) -> None:
    app, handler, scope, body = _setup()
    events = 0

    async def broken_receive():
        nonlocal events
        events += 1
        if after_fragment and events == 1:
            return {"type": "http.request", "body": body[:7], "more_body": True}
        raise error("private-session-token and /sensitive/database.sqlite")

    output = _invoke(app, scope, broken_receive)
    assert len(output) == 2
    assert output[0]["status"] == 500
    result = json.loads(output[1]["body"])
    assert result["code"] == "request_receive_failed"
    assert b"private-session-token" not in output[1]["body"]
    assert b"database.sqlite" not in output[1]["body"]
    assert handler.calls == 0

    # A later valid request on the same canonical adapter still works;
    # the failed partial request cannot leak a pending command/effect.
    async def healthy_receive():
        return {"type": "http.request", "body": body, "more_body": False}

    recovered = _invoke(app, scope, healthy_receive)
    assert recovered[0]["status"] == 200
    assert handler.calls == 1


def test_host_cancellation_propagates_without_dispatch_or_response() -> None:
    app, handler, scope, _body = _setup()
    output = []

    async def cancelled_receive():
        raise asyncio.CancelledError("host cancelled before dispatch")

    async def send(event):
        output.append(event)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(app(scope, cancelled_receive, send))
    assert output == []
    assert handler.calls == 0
