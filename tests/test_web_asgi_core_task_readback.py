"""Plan 6 §1: end-to-end ASGI ingress to the existing durable Core TaskQueue.

The test-only authorization fixture is *not* a production tenant/account
authority. It proves request routing, minimal projection, isolation and restart
against the incumbent SQLiteStore rather than a second Web task database.
"""
from __future__ import annotations

import asyncio
import json

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.web_api import WebApplicationBoundary, WebPrincipal
from nika_core.web_api.asgi import ASGICommandApplication
from nika_core.web_api.http_transport import HttpCommandAdapter
from nika_core.web_api.task_queries import WebTaskQueryHandler


class _FixtureAuthorization:
    def allows(self, principal, command) -> bool:
        return (
            principal.tenant_id == "tenant-a"
            and principal.user_id == "user-a"
            and principal.workspace_id == "workspace-a"
            and command.action_id == "task.inspect"
        )


def _principal(tenant: str = "tenant-a") -> WebPrincipal:
    return WebPrincipal(
        tenant_id=tenant,
        user_id="user-a",
        workspace_id="workspace-a",
        session_id="server-verified-session-a",
    )


def _app(queue: TaskQueue) -> ASGICommandApplication:
    return ASGICommandApplication(
        HttpCommandAdapter(WebApplicationBoundary(
            authorization=_FixtureAuthorization(),
            handler=WebTaskQueryHandler(queue),
        )),
        allowed_origins=frozenset({"https://nika.example"}),
    )


def _query(
    app: ASGICommandApplication,
    principal: WebPrincipal,
    task_id: str,
    *,
    host: bytes = b"nika.example",
) -> tuple[int, dict[str, object]]:
    body = json.dumps({
        "request_id": "stable-query-id",
        "action_id": "task.inspect",
        "payload": {"task_id": task_id},
    }).encode("utf-8")
    scope = {
        "type": "http",
        "scheme": "https",
        "method": "POST",
        "path": "/v1/commands",
        "query_string": b"",
        "state": {"nika_principal": principal},
        "headers": [
            (b"host", host),
            (b"origin", b"https://nika.example"),
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ],
    }
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    asyncio.run(app(scope, receive, send))
    assert len(sent) == 2
    return int(sent[0]["status"]), json.loads(sent[1]["body"].decode("utf-8"))


def test_real_https_core_query_keeps_minimal_projection_after_restart(tmp_path) -> None:
    db = tmp_path / "nika.db"
    store = SQLiteStore(db)
    store.initialize()
    task = TaskQueue(store).create(
        workspace_id="workspace-a",
        agent_id="internal-agent-not-for-web",
        payload={"private_token": "secret-must-not-leak"},
    )
    status, data = _query(_app(TaskQueue(store)), _principal(), task.task_id)
    assert status == 200
    assert data["data"] == {"task_id": task.task_id, "state": "CREATED"}
    assert "secret-must-not-leak" not in json.dumps(data)
    assert "internal-agent-not-for-web" not in json.dumps(data)

    reopened = SQLiteStore(db)
    reopened.initialize()
    status, after_restart = _query(
        _app(TaskQueue(reopened)), _principal(), task.task_id,
    )
    assert status == 200
    assert after_restart["data"] == data["data"]


def test_real_https_cross_workspace_and_absent_id_are_indistinguishable(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    queue = TaskQueue(store)
    foreign = queue.create(
        workspace_id="workspace-b",
        agent_id="foreign-agent",
        payload={"secret": "workspace-b-only"},
    )
    app = _app(queue)
    cross = _query(app, _principal(), foreign.task_id)
    missing = _query(app, _principal(), "absent-id")
    assert cross == missing
    assert cross[0] == 409
    assert cross[1]["code"] == "not_found"
    assert "workspace-b-only" not in json.dumps(cross)


def test_https_server_tenant_and_host_denial_do_not_inspect_core(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    task = TaskQueue(store).create(workspace_id="workspace-a", agent_id="a")
    app = _app(TaskQueue(store))
    forbidden = _query(app, _principal("tenant-b"), task.task_id)
    forged_host = _query(app, _principal(), task.task_id, host=b"forged.example")
    assert forbidden[0] == 403
    assert forbidden[1]["code"] == "forbidden"
    assert forged_host[0] == 403
    assert forged_host[1]["code"] == "host_forbidden"
    assert "workspace-a" not in json.dumps(forbidden)
    assert "workspace-a" not in json.dumps(forged_host)
