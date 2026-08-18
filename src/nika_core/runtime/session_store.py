from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from nika_core.data.sqlite import SQLiteStore
from nika_core.runtime.contracts import RuntimeOutcome, RuntimeResult

_RESUMABLE_OUTCOMES = frozenset(
    {
        RuntimeOutcome.WAITING_APPROVAL,
        RuntimeOutcome.PAUSED,
        RuntimeOutcome.FAILED,
    }
)


@dataclass(frozen=True, slots=True)
class RuntimeSessionRecord:
    task_id: str
    runtime_id: str
    thread_id: str
    resume_token: str
    outcome: RuntimeOutcome
    updated_at: str


class RuntimeSessionStore:
    """Nika-owned pointer from a task to framework-persisted resumable state."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def get(self, task_id: str) -> RuntimeSessionRecord | None:
        with self._store.connection() as conn:
            row = conn.execute(
                """
                SELECT task_id, runtime_id, thread_id, resume_token, outcome, updated_at
                FROM runtime_sessions
                WHERE task_id = ?
                """,
                (task_id,),
            ).fetchone()
        if row is None:
            return None
        return RuntimeSessionRecord(
            task_id=row["task_id"],
            runtime_id=row["runtime_id"],
            thread_id=row["thread_id"],
            resume_token=row["resume_token"],
            outcome=RuntimeOutcome(row["outcome"]),
            updated_at=row["updated_at"],
        )

    def list_resumable(self) -> tuple[RuntimeSessionRecord, ...]:
        with self._store.connection() as conn:
            rows = conn.execute(
                """
                SELECT task_id, runtime_id, thread_id, resume_token, outcome, updated_at
                FROM runtime_sessions
                ORDER BY updated_at, task_id
                """
            ).fetchall()
        return tuple(
            RuntimeSessionRecord(
                task_id=row["task_id"],
                runtime_id=row["runtime_id"],
                thread_id=row["thread_id"],
                resume_token=row["resume_token"],
                outcome=RuntimeOutcome(row["outcome"]),
                updated_at=row["updated_at"],
            )
            for row in rows
        )

    def record_result(
        self,
        *,
        task_id: str,
        runtime_id: str,
        thread_id: str,
        result: RuntimeResult,
    ) -> None:
        if result.outcome not in _RESUMABLE_OUTCOMES or not result.resume_token:
            self.delete(task_id)
            return

        now = datetime.now(UTC).isoformat()
        with self._store.connection() as conn:
            conn.execute(
                """
                INSERT INTO runtime_sessions(
                    task_id, runtime_id, thread_id, resume_token, outcome, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    runtime_id = excluded.runtime_id,
                    thread_id = excluded.thread_id,
                    resume_token = excluded.resume_token,
                    outcome = excluded.outcome,
                    updated_at = excluded.updated_at
                """,
                (
                    task_id,
                    runtime_id,
                    thread_id,
                    result.resume_token,
                    result.outcome.value,
                    now,
                ),
            )

    def delete(self, task_id: str) -> None:
        with self._store.connection() as conn:
            conn.execute("DELETE FROM runtime_sessions WHERE task_id = ?", (task_id,))
