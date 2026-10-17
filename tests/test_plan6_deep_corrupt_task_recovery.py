"""Plan 6 §1: deep corrupt Core payloads are definite, scoped read failures."""
from __future__ import annotations

import json

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.web_api import WebApplicationBoundary, WebPrincipal
from nika_core.web_api.http_transport import HttpCommandAdapter
from nika_core.web_api.task_queries import WebTaskQueryHandler


class _InspectAuthority:
    def allows(self, principal, command) -> bool:
        return (
            principal.tenant_id == "tenant-a"
            and command.action_id == "task.inspect"
        )


def _principal(workspace: str) -> WebPrincipal:
    return WebPrincipal(
        tenant_id="tenant-a",
        user_id="user-a",
        workspace_id=workspace,
        session_id="server-session-a",
    )


def _inspect(queue: TaskQueue, workspace: str, task_id: str):
    adapter = HttpCommandAdapter(WebApplicationBoundary(
        authorization=_InspectAuthority(),
        handler=WebTaskQueryHandler(queue),
    ))
    return adapter.handle(
        principal=_principal(workspace),
        method="POST",
        content_type="application/json",
        body=json.dumps({
            "request_id": "same-request",
            "action_id": "task.inspect",
            "payload": {"task_id": task_id},
        }).encode("utf-8"),
    )


def test_recursion_corrupt_task_is_opaque_to_foreign_workspace_and_recovers(tmp_path) -> None:
    path = tmp_path / "nika.sqlite"
    store = SQLiteStore(path)
    store.initialize()
    queue = TaskQueue(store)
    task = queue.create(
        workspace_id="workspace-a",
        agent_id="agent-a",
        payload={"secret": "must-never-escape"},
    )
    # The canonical TaskQueue.get uses json.loads before projecting workspace.
    # A syntactically valid but deeply nested JSON value can raise
    # RecursionError, which must never be classified as an unknown write.
    deep_json = "[" * 1800 + "0" + "]" * 1800
    with store.connection() as conn:
        conn.execute(
            "UPDATE tasks SET payload_json = ? WHERE task_id = ?",
            (deep_json, task.task_id),
        )

    foreign = _inspect(queue, "workspace-b", task.task_id)
    absent = _inspect(queue, "workspace-b", "missing-task")
    assert foreign.status_code == absent.status_code == 409
    assert foreign.body == absent.body
    assert json.loads(foreign.body)["code"] == "not_found"

    owner = _inspect(queue, "workspace-a", task.task_id)
    outcome = json.loads(owner.body)
    assert owner.status_code == 200
    assert outcome["status"] == "failed"
    assert outcome["code"] == "storage_unavailable"
    assert outcome["request_id"] == "same-request"
    assert outcome["data"] == {}
    assert b"outcome_unknown" not in owner.body
    assert b"must-never-escape" not in owner.body

    with store.connection() as conn:
        conn.execute(
            "UPDATE tasks SET payload_json = ? WHERE task_id = ?",
            ("{}", task.task_id),
        )
    reopened = SQLiteStore(path)
    reopened.initialize()
    recovered = _inspect(TaskQueue(reopened), "workspace-a", task.task_id)
    assert recovered.status_code == 200
    assert json.loads(recovered.body)["data"] == {
        "task_id": task.task_id,
        "state": "CREATED",
    }
