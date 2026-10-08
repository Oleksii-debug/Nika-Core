"""Plan 6 §1: foreign Core tasks never incur untrusted payload decoding."""
from __future__ import annotations

import json
import sqlite3

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_queue import TaskQueue
from nika_core.web_api import WebApplicationBoundary, WebPrincipal
from nika_core.web_api.http_transport import HttpCommandAdapter
from nika_core.web_api.task_queries import WebTaskQueryHandler


class _FixtureAuthority:
    """Test-only decision; production tenant membership is not provided here."""

    def allows(self, principal, command) -> bool:
        return principal.tenant_id == "tenant-a" and command.action_id == "task.inspect"


def _inspect(queue: TaskQueue, workspace: str, task_id: str):
    adapter = HttpCommandAdapter(WebApplicationBoundary(
        authorization=_FixtureAuthority(), handler=WebTaskQueryHandler(queue),
    ))
    return adapter.handle(
        principal=WebPrincipal(
            tenant_id="tenant-a", user_id="user-a",
            workspace_id=workspace, session_id="server-session-a",
        ),
        method="POST", content_type="application/json",
        body=json.dumps({
            "request_id": "read-1", "action_id": "task.inspect",
            "payload": {"task_id": task_id},
        }).encode("utf-8"),
    )


def _queue(tmp_path):
    path = tmp_path / "nika.sqlite"
    store = SQLiteStore(path)
    store.initialize()
    return path, store, TaskQueue(store)


def test_foreign_and_missing_rows_bypass_canonical_payload_decoder(
    tmp_path, monkeypatch,
) -> None:
    _, store, queue = _queue(tmp_path)
    task = queue.create(
        workspace_id="workspace-a", agent_id="agent-a",
        payload={"secret": "private-tenant-payload"},
    )
    with store.connection() as conn:
        conn.execute(
            "UPDATE tasks SET payload_json = ? WHERE task_id = ?",
            ("[" * 1800 + "0" + "]" * 1800, task.task_id),
        )

    def forbidden_decode(_task_id: str):
        raise AssertionError("foreign and absent rows must not parse Core JSON")

    monkeypatch.setattr(queue, "get", forbidden_decode)
    foreign = _inspect(queue, "workspace-b", task.task_id)
    missing = _inspect(queue, "workspace-b", "missing-task")
    assert foreign.status_code == missing.status_code == 409
    assert foreign.body == missing.body
    assert json.loads(foreign.body)["code"] == "not_found"
    assert b"private-tenant-payload" not in foreign.body


def test_owner_still_uses_canonical_task_queue_and_reopens(tmp_path, monkeypatch) -> None:
    path, _, queue = _queue(tmp_path)
    task = queue.create(workspace_id="workspace-a", agent_id="agent-a")
    original_get = queue.get
    calls: list[str] = []

    def counted_get(task_id: str):
        calls.append(task_id)
        return original_get(task_id)

    monkeypatch.setattr(queue, "get", counted_get)
    owned = _inspect(queue, "workspace-a", task.task_id)
    assert owned.status_code == 200
    assert json.loads(owned.body)["data"] == {
        "task_id": task.task_id, "state": "CREATED",
    }
    assert calls == [task.task_id]
    reopened = SQLiteStore(path)
    reopened.initialize()
    result = _inspect(TaskQueue(reopened), "workspace-a", task.task_id)
    assert result.status_code == 200
    assert json.loads(result.body)["data"]["state"] == "CREATED"


def test_preflight_sqlite_fault_is_secret_free_and_recoverable(
    tmp_path, monkeypatch,
) -> None:
    path, store, queue = _queue(tmp_path)
    task = queue.create(workspace_id="workspace-a", agent_id="agent-a")

    def broken_connection():
        raise sqlite3.OperationalError("private-db-path and secret-password")

    monkeypatch.setattr(store, "connection", broken_connection)
    failed = _inspect(queue, "workspace-a", task.task_id)
    assert failed.status_code == 200
    assert json.loads(failed.body)["code"] == "storage_unavailable"
    assert b"private-db-path" not in failed.body
    assert b"secret-password" not in failed.body
    reopened = SQLiteStore(path)
    reopened.initialize()
    recovered = _inspect(TaskQueue(reopened), "workspace-a", task.task_id)
    assert json.loads(recovered.body)["data"]["task_id"] == task.task_id


def test_task_transfer_during_canonical_read_remains_foreign(
    tmp_path, monkeypatch,
) -> None:
    _, store, queue = _queue(tmp_path)
    task = queue.create(workspace_id="workspace-a", agent_id="agent-a")
    original_get = queue.get

    def transfer_then_read(task_id: str):
        with store.connection() as conn:
            conn.execute(
                "UPDATE tasks SET workspace_id = ? WHERE task_id = ?",
                ("workspace-b", task_id),
            )
        return original_get(task_id)

    monkeypatch.setattr(queue, "get", transfer_then_read)
    foreign = _inspect(queue, "workspace-a", task.task_id)
    missing = _inspect(queue, "workspace-a", "missing-task")
    assert foreign.status_code == missing.status_code == 409
    assert foreign.body == missing.body


def test_corrupt_task_transfer_after_preflight_is_not_owner_error_oracle(
    tmp_path, monkeypatch,
) -> None:
    _, store, queue = _queue(tmp_path)
    task = queue.create(workspace_id="workspace-a", agent_id="agent-a")

    def transfer_then_corrupt(task_id: str):
        with store.connection() as conn:
            conn.execute(
                "UPDATE tasks SET workspace_id = ?, payload_json = ? "
                "WHERE task_id = ?",
                ("workspace-b", '{"secret":', task_id),
            )
        return queue_original_get(task_id)

    queue_original_get = queue.get
    monkeypatch.setattr(queue, "get", transfer_then_corrupt)
    transferred = _inspect(queue, "workspace-a", task.task_id)
    absent = _inspect(queue, "workspace-a", "missing-task")
    assert transferred.status_code == absent.status_code == 409
    assert transferred.body == absent.body
    assert b"secret" not in transferred.body


def test_corrupt_owned_task_remains_bounded_definite_failure(
    tmp_path,
) -> None:
    _, store, queue = _queue(tmp_path)
    task = queue.create(workspace_id="workspace-a", agent_id="agent-a")
    with store.connection() as conn:
        conn.execute(
            "UPDATE tasks SET payload_json = ? WHERE task_id = ?",
            ("[" * 1800 + "0" + "]" * 1800, task.task_id),
        )
    result = _inspect(queue, "workspace-a", task.task_id)
    body = json.loads(result.body)
    assert result.status_code == 200
    assert body["status"] == "failed"
    assert body["code"] == "storage_unavailable"
    assert body["request_id"] == "read-1"
    assert body["data"] == {}
    assert b"outcome_unknown" not in result.body


def test_sqlite_fault_after_workspace_transfer_is_opaque_not_owner_error(
    tmp_path, monkeypatch,
) -> None:
    _, store, queue = _queue(tmp_path)
    task = queue.create(workspace_id="workspace-a", agent_id="agent-a")

    def transfer_then_fail(task_id: str):
        with store.connection() as conn:
            conn.execute(
                "UPDATE tasks SET workspace_id = ? WHERE task_id = ?",
                ("workspace-b", task_id),
            )
        raise sqlite3.OperationalError("private-tenant-db-path")

    monkeypatch.setattr(queue, "get", transfer_then_fail)
    moved = _inspect(queue, "workspace-a", task.task_id)
    absent = _inspect(queue, "workspace-a", "missing-task")
    assert moved.status_code == absent.status_code == 409
    assert moved.body == absent.body
    assert b"private-tenant-db-path" not in moved.body


def test_sqlite_fault_after_task_delete_is_opaque_not_owner_error(
    tmp_path, monkeypatch,
) -> None:
    _, store, queue = _queue(tmp_path)
    task = queue.create(workspace_id="workspace-a", agent_id="agent-a")

    def delete_then_fail(task_id: str):
        with store.connection() as conn:
            conn.execute("DELETE FROM task_events WHERE task_id = ?", (task_id,))
            conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
        raise sqlite3.DatabaseError("deleted-private-task-state")

    monkeypatch.setattr(queue, "get", delete_then_fail)
    missing = _inspect(queue, "workspace-a", task.task_id)
    absent = _inspect(queue, "workspace-a", "missing-task")
    assert missing.status_code == absent.status_code == 409
    assert missing.body == absent.body
    assert b"deleted-private-task-state" not in missing.body


def test_sqlite_fault_for_still_owned_task_is_bounded_and_recoverable(
    tmp_path, monkeypatch,
) -> None:
    path, _, queue = _queue(tmp_path)
    task = queue.create(workspace_id="workspace-a", agent_id="agent-a")

    def fail_read(_task_id: str):
        raise sqlite3.DatabaseError("credential-leak-must-not-escape")

    monkeypatch.setattr(queue, "get", fail_read)
    failed = _inspect(queue, "workspace-a", task.task_id)
    result = json.loads(failed.body)
    assert failed.status_code == 200
    assert result["status"] == "failed"
    assert result["code"] == "storage_unavailable"
    assert result["request_id"] == "read-1"
    assert result["data"] == {}
    assert b"credential-leak-must-not-escape" not in failed.body
    reopened = SQLiteStore(path)
    reopened.initialize()
    recovered = _inspect(TaskQueue(reopened), "workspace-a", task.task_id)
    assert recovered.status_code == 200
    assert json.loads(recovered.body)["data"]["state"] == "CREATED"


def test_task_transfer_after_successful_core_read_is_still_opaque(
    tmp_path, monkeypatch,
) -> None:
    """A parsed snapshot cannot authorize a task that moved before projection."""
    _, store, queue = _queue(tmp_path)
    task = queue.create(workspace_id="workspace-a", agent_id="agent-a")
    original_get = queue.get

    def read_then_transfer(task_id: str):
        snapshot = original_get(task_id)
        with store.connection() as conn:
            conn.execute(
                "UPDATE tasks SET workspace_id = ? WHERE task_id = ?",
                ("workspace-b", task_id),
            )
        return snapshot

    monkeypatch.setattr(queue, "get", read_then_transfer)
    transferred = _inspect(queue, "workspace-a", task.task_id)
    absent = _inspect(queue, "workspace-a", "absent-task")
    assert transferred.status_code == absent.status_code == 409
    assert transferred.body == absent.body


def test_task_delete_after_successful_core_read_is_still_opaque(
    tmp_path, monkeypatch,
) -> None:
    """A deleted task cannot leak its former state from a parsed snapshot."""
    _, store, queue = _queue(tmp_path)
    task = queue.create(workspace_id="workspace-a", agent_id="agent-a")
    original_get = queue.get

    def read_then_delete(task_id: str):
        snapshot = original_get(task_id)
        with store.connection() as conn:
            conn.execute("DELETE FROM task_events WHERE task_id = ?", (task_id,))
            conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
        return snapshot

    monkeypatch.setattr(queue, "get", read_then_delete)
    deleted = _inspect(queue, "workspace-a", task.task_id)
    absent = _inspect(queue, "workspace-a", "absent-task")
    assert deleted.status_code == absent.status_code == 409
    assert deleted.body == absent.body


def test_post_read_membership_sqlite_failure_is_definite_and_secret_free(
    tmp_path, monkeypatch,
) -> None:
    """Final metadata fault does not publish the already-decoded task state."""
    path, store, queue = _queue(tmp_path)
    task = queue.create(workspace_id="workspace-a", agent_id="agent-a")
    original_connection = store.connection
    calls = 0

    def fail_only_post_read():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise sqlite3.OperationalError("secret-final-membership-db-path")
        return original_connection()

    monkeypatch.setattr(store, "connection", fail_only_post_read)
    failed = _inspect(queue, "workspace-a", task.task_id)
    decoded = json.loads(failed.body)
    assert calls == 3
    assert failed.status_code == 200
    assert decoded["status"] == "failed"
    assert decoded["code"] == "storage_unavailable"
    assert decoded["data"] == {}
    assert b"secret-final-membership-db-path" not in failed.body
    reopened = SQLiteStore(path)
    reopened.initialize()
    recovered = _inspect(TaskQueue(reopened), "workspace-a", task.task_id)
    assert recovered.status_code == 200
    assert json.loads(recovered.body)["data"]["state"] == "CREATED"
