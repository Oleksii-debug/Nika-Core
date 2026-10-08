"""Plan 6 §1: actual Core TaskQueue Web query, isolation and restart checks."""
from __future__ import annotations

import json

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.web_api import WebApplicationBoundary, WebPrincipal
from nika_core.web_api.http_transport import HttpCommandAdapter
from nika_core.web_api.task_queries import WebTaskQueryHandler


class _ServerAuthority:
    def allows(self, principal, command) -> bool:
        return (
            principal.tenant_id == "tenant-a"
            and principal.workspace_id in {"workspace-a", "workspace-b"}
            and command.action_id == "task.inspect"
        )


def _principal(workspace: str = "workspace-a", tenant: str = "tenant-a"):
    return WebPrincipal(
        tenant_id=tenant, user_id="user-a", workspace_id=workspace,
        session_id="server-session-a",
    )


def _inspect(adapter, principal, task_id, *, request_id="query-1"):
    return adapter.handle(
        principal=principal, method="POST", content_type="application/json",
        body=json.dumps({
            "request_id":request_id, "action_id":"task.inspect",
            "payload":{"task_id":task_id},
        }).encode("utf-8"),
    )


def _adapter(queue):
    return HttpCommandAdapter(WebApplicationBoundary(
        authorization=_ServerAuthority(), handler=WebTaskQueryHandler(queue),
    ))


def test_real_task_queue_query_is_scoped_and_excludes_payload(tmp_path) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    queue = TaskQueue(store)
    first = queue.create(
        workspace_id="workspace-a", agent_id="agent-a",
        payload={"private_token": "never-return-this-secret"},
    )
    other = queue.create(
        workspace_id="workspace-b", agent_id="agent-b",
        payload={"message": "private-workspace-b"},
    )
    adapter = _adapter(queue)
    result = _inspect(adapter, _principal(), first.task_id)
    assert result.status_code == 200
    response = json.loads(result.body)
    assert response["data"] == {"task_id": first.task_id, "state": "created"}
    assert b"never-return-this-secret" not in result.body
    assert b"agent-a" not in result.body

    cross = _inspect(adapter, _principal(), other.task_id)
    missing = _inspect(adapter, _principal(), "absent-task")
    assert cross.status_code == missing.status_code == 409
    assert json.loads(cross.body)["code"] == json.loads(missing.body)["code"] == "not_found"
    assert b"private-workspace-b" not in cross.body

    forbidden = _inspect(adapter, _principal(tenant="tenant-b"), first.task_id)
    assert forbidden.status_code == 403
    assert json.loads(forbidden.body)["code"] == "forbidden"


def test_query_survives_process_style_store_recreation(tmp_path) -> None:
    db = tmp_path / "nika.db"
    store = SQLiteStore(db)
    store.initialize()
    task_id = TaskQueue(store).create(
        workspace_id="workspace-a", agent_id="agent-a",
    ).task_id
    reopened = SQLiteStore(db)
    reopened.initialize()
    result = _inspect(_adapter(TaskQueue(reopened)), _principal(), task_id)
    assert result.status_code == 200
    assert json.loads(result.body)["data"]["task_id"] == task_id


@pytest.mark.parametrize("bad_id", ["", " ", " 123", "123 ", "bad\nkey", "x" * 121])
def test_invalid_query_never_reads_cross_workspace_records(tmp_path, bad_id) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    queue = TaskQueue(store)
    adapter = _adapter(queue)
    result = _inspect(adapter, _principal(), bad_id)
    assert result.status_code == 409
    assert json.loads(result.body)["code"] == "invalid_query"


def test_task_query_rejects_noncanonical_queue() -> None:
    with pytest.raises(ValueError, match="canonical Nika TaskQueue"):
        WebTaskQueryHandler(object())  # type: ignore[arg-type]
