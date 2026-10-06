from __future__ import annotations

import json
import math
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_state import TaskState, require_transition


class TaskPayloadCorruptionError(ValueError):
    """Stored task payload cannot safely be interpreted as a command."""


def _unique_payload_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate task payload JSON field")
        result[key] = value
    return result


def _reject_nonfinite_constant(_value: str) -> object:
    raise ValueError("non-finite task JSON constant")


def _finite_json_float(raw: str) -> float:
    number = float(raw)
    if not math.isfinite(number):
        raise ValueError("non-finite task JSON float")
    return number


def decode_task_payload(raw: object) -> dict[str, object]:
    error = "Збережені дані завдання пошкоджені."
    # SQLite TEXT affinity does not prevent external writes of BLOB values.
    if type(raw) is not str:
        raise TaskPayloadCorruptionError(error)
    try:
        payload = json.loads(
            raw,
            object_pairs_hook=_unique_payload_object,
            parse_constant=_reject_nonfinite_constant,
            parse_float=_finite_json_float,
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise TaskPayloadCorruptionError(error) from exc
    # A list, null or string is valid JSON but not a TaskRecord payload. Never
    # allow a downstream consumer to interpret it as missing legacy settings.
    if type(payload) is not dict:
        raise TaskPayloadCorruptionError(error)
    return payload


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
                    json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False),
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

    def list_by_states(
        self,
        states: tuple[TaskState, ...],
        *,
        limit: int = 50,
    ) -> tuple[TaskRecord, ...]:
        """Return recent matching states after filtering the entire durable task table."""
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        if any(type(state) is not TaskState for state in states):
            raise TypeError("states must contain only TaskState values")
        unique_states = tuple(dict.fromkeys(states))
        if not unique_states:
            return ()
        placeholders = ", ".join("?" for _ in unique_states)
        statement = (
            "SELECT task_id, workspace_id, agent_id, state, payload_json "
            f"FROM tasks WHERE state IN ({placeholders}) "
            "ORDER BY updated_at DESC, created_at DESC LIMIT ?"
        )
        parameters = (*tuple(state.value for state in unique_states), limit)
        with self.store.connection() as conn:
            rows = conn.execute(statement, parameters).fetchall()
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
            payload=decode_task_payload(row["payload_json"]),
        )
