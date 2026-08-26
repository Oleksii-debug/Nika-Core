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
        return self._create_exact(
            task_id=str(uuid.uuid4()),
            workspace_id=workspace_id,
            agent_id=agent_id,
            payload=dict(payload or {}),
            replay_allowed=False,
        )

    def create_exact(
        self,
        *,
        task_id: str,
        workspace_id: str,
        agent_id: str,
        payload: dict[str, object] | None = None,
    ) -> TaskRecord:
        """Create or replay one host-owned exact task identity.

        Product integrations sometimes derive a deterministic task identity before a durable
        child subsystem can reference it. Exact replay is allowed only when immutable task
        ownership and payload are identical; an existing conflicting identity fails closed.
        """

        if not task_id.strip() or task_id != task_id.strip():
            raise ValueError("exact task_id must be normalized and non-empty")
        if not workspace_id.strip() or not agent_id.strip():
            raise ValueError("exact task workspace and agent identity must not be empty")
        return self._create_exact(
            task_id=task_id,
            workspace_id=workspace_id,
            agent_id=agent_id,
            payload=dict(payload or {}),
            replay_allowed=True,
        )

    def _create_exact(
        self,
        *,
        task_id: str,
        workspace_id: str,
        agent_id: str,
        payload: dict[str, object],
        replay_allowed: bool,
    ) -> TaskRecord:
        now = datetime.now(UTC).isoformat()
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        with self.store.connection() as conn:
            if replay_allowed:
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO tasks(
                        task_id, workspace_id, agent_id, state, payload_json, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        workspace_id,
                        agent_id,
                        TaskState.CREATED.value,
                        payload_json,
                        now,
                        now,
                    ),
                )
            else:
                cursor = conn.execute(
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
                        payload_json,
                        now,
                        now,
                    ),
                )
            if cursor.rowcount == 1:
                conn.execute(
                    """
                    INSERT INTO task_events(task_id, previous_state, new_state, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (task_id, None, TaskState.CREATED.value, now),
                )
            row = conn.execute(
                "SELECT task_id, workspace_id, agent_id, state, payload_json "
                "FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError("exact task insertion did not produce a durable task")
            record = self._record_from_row(row)
            if replay_allowed and (
                record.workspace_id != workspace_id
                or record.agent_id != agent_id
                or record.payload != payload
            ):
                raise ValueError("exact task_id conflicts with existing task identity")
            return record

    def get(self, task_id: str) -> TaskRecord:
        with self.store.connection() as conn:
            row = conn.execute(
                "SELECT task_id, workspace_id, agent_id, state, payload_json "
                "FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"Unknown task: {task_id}")
        return self._record_from_row(row)

    def list_recent(self, *, limit: int = 50) -> tuple[TaskRecord, ...]:
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        with self.store.connection() as conn:
            rows = conn.execute(
                "SELECT task_id, workspace_id, agent_id, state, payload_json "
                "FROM tasks ORDER BY updated_at DESC, created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(self._record_from_row(row) for row in rows)

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

    @staticmethod
    def _record_from_row(row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            task_id=row["task_id"],
            workspace_id=row["workspace_id"],
            agent_id=row["agent_id"],
            state=TaskState(row["state"]),
            payload=json.loads(row["payload_json"]),
        )