"""Read-only Web projection of canonical Nika TaskQueue records.

This is not an alternate Web task store or a cloud job scheduler. A server-owned
WebAuthorizationPort must validate the principal's tenant/workspace ownership
and entitlement before the WebApplicationBoundary calls this handler.
"""
from __future__ import annotations

import sqlite3

from nika_core.kernel.task_queue import TaskQueue
from nika_core.web_api.contracts import WebCommand, WebCommandResult, WebPrincipal


class WebTaskQueryHandler:
    """Project one scoped durable Core task without exposing stored task payloads."""

    def __init__(self, queue: TaskQueue) -> None:
        if type(queue) is not TaskQueue:
            raise ValueError("use the canonical Nika TaskQueue")
        self._queue = queue

    def handle(self, principal: WebPrincipal, command: WebCommand) -> WebCommandResult:
        if type(principal) is not WebPrincipal or type(command) is not WebCommand:
            raise ValueError("trusted Web carriers are required")
        if command.action_id != "task.inspect":
            return self._reject(command.request_id, "unsupported_action")
        payload = command.payload
        if set(payload) != {"task_id"} or type(payload["task_id"]) is not str:
            return self._reject(command.request_id, "invalid_query")
        task_id = payload["task_id"]
        if (
            not task_id
            or task_id != task_id.strip()
            or len(task_id) > 120
            # Canonical TaskQueue IDs are ASCII; accepting a printable
            # lookalike Unicode identifier creates a screen-reader spoofing
            # and unnecessary SQLite lookup surface for untrusted Web clients.
            or not task_id.isascii()
            or any(not ch.isprintable() or ch.isspace() for ch in task_id)
        ):
            return self._reject(command.request_id, "invalid_query")
        # TaskQueue.get deserializes payload_json before returning workspace
        # identity. Reject absent/foreign rows using the canonical SQLite store
        # *before* paying for untrusted JSON decoding; a huge corrupt foreign
        # payload must never consume the owner's Web request budget.
        # This is a projection-only membership fence, NOT tenant authorization.
        # The server WebAuthorizationPort must authorize the principal first.
        try:
            with self._queue.store.connection() as conn:
                owner = conn.execute(
                    "SELECT workspace_id FROM tasks WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
        except sqlite3.Error:
            return self._storage_failure(command.request_id)
        if owner is None or owner["workspace_id"] != principal.workspace_id:
            return self._reject(command.request_id, "not_found")
        try:
            record = self._queue.get(task_id)
        except KeyError:
            # Deleted between scoped preflight and canonical read.
            return self._reject(command.request_id, "not_found")
        except (sqlite3.Error, ValueError, TypeError, RecursionError):
            # A canonical read may fail *after* the preflight row moved to
            # another workspace or was deleted. The error class is not
            # evidence of continued ownership. Recheck only canonical
            # workspace metadata before returning a storage failure, so a
            # transferred or deleted record remains indistinguishable from
            # an absent record. This read-only handler performs no retry.
            try:
                with self._queue.store.connection() as conn:
                    current = conn.execute(
                        "SELECT workspace_id FROM tasks WHERE task_id = ?",
                        (task_id,),
                    ).fetchone()
            except sqlite3.Error:
                return self._storage_failure(command.request_id)
            if current is None or current["workspace_id"] != principal.workspace_id:
                return self._reject(command.request_id, "not_found")
            return self._storage_failure(command.request_id)
        if record.workspace_id != principal.workspace_id:
            # Never reveal foreign task metadata or its payload health.
            return self._reject(command.request_id, "not_found")
        # TaskQueue.get reads and decodes outside the preflight SQLite
        # connection. Workspace ownership can change after it returns a
        # valid TaskRecord. Revalidate against the canonical store before
        # emitting even the minimal task-state projection.
        try:
            with self._queue.store.connection() as conn:
                current = conn.execute(
                    "SELECT workspace_id FROM tasks WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
        except sqlite3.Error:
            return self._storage_failure(command.request_id)
        if current is None or current["workspace_id"] != principal.workspace_id:
            return self._reject(command.request_id, "not_found")
        if type(record.payload) is not dict:
            # Canonical TaskRecord payload requires a JSON object. A valid
            # JSON scalar/list is damaged state, not a healthy task.
            return self._storage_failure(command.request_id)
        return WebCommandResult.create(
            request_id=command.request_id,
            status="completed",
            code="ok",
            message="Task state is available.",
            data={"task_id":record.task_id, "state":record.state.value},
        )

    @staticmethod
    def _storage_failure(request_id: str) -> WebCommandResult:
        return WebCommandResult.create(
            request_id=request_id,
            status="failed",
            code="storage_unavailable",
            message="Task state is temporarily unavailable.",
        )

    @staticmethod
    def _reject(request_id: str, code: str) -> WebCommandResult:
        return WebCommandResult.create(
            request_id=request_id,
            status="rejected",
            code=code,
            message="Task query is unavailable.",
        )
