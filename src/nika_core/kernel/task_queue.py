from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_state import TaskState, require_transition


@dataclass(frozen=True, slots=True)
class TaskRecord:
    task_id: str
    workspace_id: str
    agent_id: str
    state: TaskState
    payload: dict[str, object]


class TaskQueue:
    def __init__(self, store: SQLiteStore) -> None:
        self.store = store

    @property
    def count_ready(self) -> int:
        with self.store.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE state = ?",
                (TaskState.READY.value,),
            ).fetchone()
        return int(row[0])

    def create(
        self,
        *,
        workspace_id: str,
        agent_id: str,
        payload: dict[str, object] | None = None,
    ) -> TaskRecord:
        task_id = str(uuid.uuid4())
        now = datetime.now(UTC).isoformat()
        payload = dict(payload or {})
        with self.store.connection() as conn:
            conn.execute(
                """
                INSERT INTO tasks(
                    task_id, workspace_id, agent_id, state, payload_json, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    workspace_id,
                    agent_id,
                    TaskState.CREATED.value,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO task_events(task_id, previous_state, new_state, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (task_id, None, TaskState.CREATED.value, now),
            )
        return TaskRecord(task_id, workspace_id, agent_id, TaskState.CREATED, payload)

    def transition(self, task_id: str, target: TaskState) -> TaskState:
        with self.store.connection() as conn:
            return self.transition_with_connection(conn, task_id, target)

    def transition_with_connection(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        target: TaskState,
    ) -> TaskState:
        """Transition a task using a caller-owned SQLite transaction.

        This is intentionally small: higher-level crash-consistency boundaries may need a
        task transition and another Nika-owned durable record to commit atomically. The
        caller owns commit/rollback through ``SQLiteStore.connection()``.
        """
        row = conn.execute(
            "SELECT state FROM tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown task: {task_id}")
        current = TaskState(row[0])
        require_transition(current, target)
        now = datetime.now(UTC).isoformat()
        conn.execute(
            "UPDATE tasks SET state = ?, updated_at = ? WHERE task_id = ?",
            (target.value, now, task_id),
        )
        conn.execute(
            """
            INSERT INTO task_events(task_id, previous_state, new_state, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (task_id, current.value, target.value, now),
        )
        return target
