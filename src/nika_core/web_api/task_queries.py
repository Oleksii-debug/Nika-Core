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
            or any(not ch.isprintable() or ch.isspace() for ch in task_id)
        ):
            return self._reject(command.request_id, "invalid_query")
        try:
            record = self._queue.get(task_id)
        except KeyError:
            return self._reject(command.request_id, "not_found")
        except sqlite3.Error:
            # Inspection is read-only: a database read fault is a definite
            # failed query, never an unknown task creation/cancellation effect.
            # Preserve the request ID, but do not expose file paths or SQL.
            return WebCommandResult.create(
                request_id=command.request_id,
                status="failed",
                code="storage_unavailable",
                message="Task state is temporarily unavailable.",
            )
        if record.workspace_id != principal.workspace_id:
            # Do not disclose whether another workspace owns the task.
            return self._reject(command.request_id, "not_found")
        return WebCommandResult.create(
            request_id=command.request_id,
            status="completed",
            code="ok",
            message="Task state is available.",
            data={"task_id":record.task_id, "state":record.state.value},
        )

    @staticmethod
    def _reject(request_id: str, code: str) -> WebCommandResult:
        return WebCommandResult.create(
            request_id=request_id,
            status="rejected",
            code=code,
            message="Task query is unavailable.",
        )
