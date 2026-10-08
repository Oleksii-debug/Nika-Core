"""Plan 6 §1: actual Core TaskQueue Web query, isolation and restart checks."""
from __future__ import annotations

import json
import sqlite3

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
    assert response["data"] == {"task_id": first.task_id, "state": "CREATED"}
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


def test_database_read_error_is_bounded_definite_failure_not_unknown_effect(
    tmp_path, monkeypatch,
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    queue = TaskQueue(store)

    def broken_query(_task_id: str):
        raise sqlite3.DatabaseError("private-path.sqlite private-token test fault")

    monkeypatch.setattr(queue, "get", broken_query)
    response = _inspect(_adapter(queue), _principal(), "any-id")
    result = json.loads(response.body)
    assert response.status_code == 200
    assert result["status"] == "failed"
    assert result["code"] == "storage_unavailable"
    assert result["request_id"] == "query-1"
    assert result["data"] == {}
    assert b"private-path" not in response.body
    assert b"private-token" not in response.body


def test_corrupt_canonical_task_payload_is_sanitized_not_unknown_effect(
    tmp_path,
) -> None:
    store = SQLiteStore(tmp_path / "nika.db")
    store.initialize()
    queue = TaskQueue(store)
    record = queue.create(
        workspace_id="workspace-a", agent_id="agent-a",
        payload={"private_token": "secret-must-not-escape"},
    )
    # Simulate a damaged SQLite record on disk: TaskQueue.get deserializes
    # payload_json before WebTaskQueryHandler projects public task state.
    with store.connection() as conn:
        conn.execute(
            "UPDATE tasks SET payload_json = ? WHERE task_id = ?",
            ('{"private_token":"do-not-leak",', record.task_id),
        )
    response = _inspect(_adapter(queue), _principal(), record.task_id)
    body = json.loads(response.body)
    assert response.status_code == 200
    assert body["status"] == "failed"
    assert body["code"] == "storage_unavailable"
    assert body["request_id"] == "query-1"
    assert body["data"] == {}
    assert b"do-not-leak" not in response.body
    assert b"secret-must-not-escape" not in response.body
    assert b"outcome_unknown" not in response.body


def test_read_only_failure_does_not_poison_recovery_after_sqlite_reopen(
    tmp_path, monkeypatch,
) -> None:
    path = tmp_path / "nika.db"
    store = SQLiteStore(path)
    store.initialize()
    queue = TaskQueue(store)
    record = queue.create(workspace_id="workspace-a", agent_id="agent-a")

    def fail_once(_task_id: str):
        raise sqlite3.OperationalError("temporary read fault")

    monkeypatch.setattr(queue, "get", fail_once)
    failed = _inspect(_adapter(queue), _principal(), record.task_id)
    assert failed.status_code == 200
    assert json.loads(failed.body)["code"] == "storage_unavailable"

    second = SQLiteStore(path)
    second.initialize()
    recovered = _inspect(_adapter(TaskQueue(second)), _principal(), record.task_id)
    assert recovered.status_code == 200
    assert json.loads(recovered.body)["data"] == {
        "task_id": record.task_id, "state": "CREATED",
    }
