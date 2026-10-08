"""Plan 6 Section 1: request-wide slow-client admission, without Core effects."""
from __future__ import annotations

import asyncio
import json

import pytest

from nika_core.web_api import WebApplicationBoundary, WebCommandResult, WebPrincipal
from nika_core.web_api import asgi as asgi_module
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
            message="Готово.",
        )


def _fixture():
    effect = _Effect()
    app = ASGICommandApplication(
        HttpCommandAdapter(WebApplicationBoundary(authorization=_Allow(), handler=effect)),
        allowed_origins=frozenset({"https://nika.example"}),
    )
    scope = {
        "type": "http", "scheme": "https", "http_version": "1.1",
        "path": "/v1/commands", "query_string": b"", "method": "POST",
        "headers": [
            (b"host", b"nika.example"),
            (b"origin", b"https://nika.example"),
            (b"content-type", b"application/json"),
        ],
        "state": {"nika_principal": WebPrincipal(
            tenant_id="tenant-a", user_id="user-a",
            workspace_id="workspace-a", session_id="session-a",
        )},
    }
    body = json.dumps({
        "request_id": "deadline-1", "action_id": "task.inspect", "payload": {},
    }).encode("utf-8")
    return app, effect, scope, body


def _invoke(app, scope, receive):
    output = []

    async def send(message):
        output.append(message)

    asyncio.run(app(scope, receive, send))
    return output


@pytest.mark.parametrize("after_fragment", [False, True])
def test_stalled_client_expires_before_core_and_recovers(monkeypatch, after_fragment):
    app, effect, scope, body = _fixture()
    monkeypatch.setattr(asgi_module, "_MAX_RECEIVE_SECONDS", 0.05)
    events = 0
    cancelled = False

    async def hanging_receive():
        nonlocal events, cancelled
        events += 1
        if after_fragment and events == 1:
            return {"type": "http.request", "body": body[:7], "more_body": True}
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled = True
            raise

    response = _invoke(app, scope, hanging_receive)
    assert response[0]["status"] == 408
    payload = json.loads(response[1]["body"])
    assert payload["code"] == "request_receive_timeout"
    assert "deadline-1" not in response[1]["body"].decode("utf-8")
    assert cancelled is True
    assert effect.calls == 0
    assert events == (2 if after_fragment else 1)

    # The failure leaves no private state or implicit retry, and a later
    # complete request still uses the same canonical handler.
    monkeypatch.setattr(asgi_module, "_MAX_RECEIVE_SECONDS", 15.0)

    async def healthy_receive():
        return {"type": "http.request", "body": body, "more_body": False}

    recovered = _invoke(app, scope, healthy_receive)
    assert recovered[0]["status"] == 200
    assert effect.calls == 1


def test_expired_deadline_never_reads_body_or_invokes_core(monkeypatch):
    app, effect, scope, _body = _fixture()
    monkeypatch.setattr(asgi_module, "_MAX_RECEIVE_SECONDS", 0.0)

    async def forbidden_receive():
        raise AssertionError("receive must not begin after deadline")

    response = _invoke(app, scope, forbidden_receive)
    assert response[0]["status"] == 408
    assert json.loads(response[1]["body"])["code"] == "request_receive_timeout"
    assert effect.calls == 0


def test_receive_timeout_exception_does_not_publish_command_outcome():
    app, effect, scope, _body = _fixture()

    async def receive():
        raise TimeoutError("attacker-controlled tokens must stay hidden")

    response = _invoke(app, scope, receive)
    assert response[0]["status"] == 408
    assert b"attacker-controlled" not in response[1]["body"]
    assert effect.calls == 0


def test_cancel_suppressing_receiver_cannot_resurrect_expired_effect(monkeypatch):
    """wait_for alone is insufficient: cancelled receive may still return a body."""
    app, effect, scope, body = _fixture()
    monkeypatch.setattr(asgi_module, "_MAX_RECEIVE_SECONDS", 0.01)

    async def late_receive():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            # ASGI host does not control behavior of every receive adapter.
            # An already-cancelled upload must not reach Core after deadline.
            await asyncio.sleep(0.02)
            return {"type": "http.request", "body": body, "more_body": False}

    response = _invoke(app, scope, late_receive)
    assert response[0]["status"] == 408
    assert json.loads(response[1]["body"])["code"] == "request_receive_timeout"
    assert effect.calls == 0

    monkeypatch.setattr(asgi_module, "_MAX_RECEIVE_SECONDS", 15.0)

    async def healthy_receive():
        return {"type": "http.request", "body": body, "more_body": False}

    response = _invoke(app, scope, healthy_receive)
    assert response[0]["status"] == 200
    assert effect.calls == 1


@pytest.mark.parametrize("late_result", [False, True])
def test_uncooperative_receive_cancellation_cannot_hold_response(monkeypatch, late_result):
    """A receiver that refuses to finish cancellation must not stall the host."""
    app, effect, scope, body = _fixture()
    monkeypatch.setattr(asgi_module, "_MAX_RECEIVE_SECONDS", 0.01)
    release = asyncio.Event()
    cancelled = asyncio.Event()

    async def hanging_receive():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
            if late_result:
                return {"type": "http.request", "body": body, "more_body": False}
            raise

    async def run_case():
        messages = []

        async def send(message):
            messages.append(message)

        # An outer host deadline catches the old wait_for behavior, which
        # could wait forever for a receiver that ignores cancellation.
        try:
            await asyncio.wait_for(app(scope, hanging_receive, send), timeout=0.5)
            await asyncio.wait_for(cancelled.wait(), timeout=0.2)
            assert messages[0]["status"] == 408
            assert json.loads(messages[1]["body"])["code"] == "request_receive_timeout"
            assert effect.calls == 0
        finally:
            # Allow the intentionally noncooperative task to finish before
            # asyncio.run() shutdown; the late body is never dispatched.
            release.set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)

    asyncio.run(run_case())
    assert effect.calls == 0
