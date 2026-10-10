from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from nika_core.data.sqlite import SQLiteStore
from nika_core.runtime.contracts import RuntimeOutcome, RuntimeResult

_ACTIVE_MARKER = "__ACTIVE__"
_RESUMABLE_OUTCOMES = frozenset(
    {
        RuntimeOutcome.WAITING_APPROVAL,
        RuntimeOutcome.PAUSED,
        RuntimeOutcome.FAILED,
    }
)
_STORED_OUTCOMES = frozenset(item.value for item in _RESUMABLE_OUTCOMES)


def _require_exact_text(
    value: object,
    *,
    field_name: str,
    non_empty: bool,
) -> str:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be exact text")
    if non_empty and not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    return value


def _resume_token_for_storage(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("runtime resume token must be text")
    snapshot = str.__str__(value)
    if not snapshot.strip():
        return None
    return snapshot


def _result_for_storage(result: object) -> tuple[RuntimeOutcome, object]:
    if type(result) is not RuntimeResult:
        raise TypeError("runtime result must be an exact RuntimeResult value")
    outcome = result.outcome
    if type(outcome) is not RuntimeOutcome:
        raise TypeError("runtime result outcome must be an exact RuntimeOutcome value")
    return outcome, result.resume_token


def _stored_text(row: sqlite3.Row, field_name: str, *, non_empty: bool = True) -> str:
    value = row[field_name]
    if type(value) is not str:
        raise RuntimeError(
            f"persisted runtime session field {field_name} has invalid SQLite storage class"
        )
    if non_empty and not value.strip():
        raise RuntimeError(f"persisted runtime session field {field_name} is empty")
    return value


def _stored_updated_at(row: sqlite3.Row) -> str:
    value = _stored_text(row, "updated_at")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise RuntimeError("persisted runtime session updated_at is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError("persisted runtime session updated_at must be timezone-aware")
    if parsed.utcoffset() != timedelta(0):
        raise RuntimeError("persisted runtime session updated_at must be UTC")
    if parsed.astimezone(UTC).isoformat() != value:
        raise RuntimeError("persisted runtime session updated_at is not canonical")
    return value


@dataclass(frozen=True, slots=True)
class RuntimeSessionRecord:
    task_id: str
    runtime_id: str
    thread_id: str
    resume_token: str
    outcome: RuntimeOutcome | None
    updated_at: str

    @property
    def is_active(self) -> bool:
        return self.outcome is None


class RuntimeSessionStore:
    """Nika-owned pointer from a task to framework-persisted resumable state."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    @staticmethod
    def _record_from_row(
        row: sqlite3.Row,
        *,
        recovery_inventory: bool = False,
    ) -> RuntimeSessionRecord:
        task_id = _stored_text(row, "task_id")
        runtime_id = _stored_text(row, "runtime_id")
        thread_id = _stored_text(row, "thread_id")
        if recovery_inventory:
            # Startup inventory must isolate an unusable/corrupt resume token as a
            # non-resumable candidate instead of aborting classification for every
            # persisted session. Never coerce BLOB/blank storage into usable authority.
            raw_resume_token = row["resume_token"]
            resume_token = (
                raw_resume_token
                if type(raw_resume_token) is str and raw_resume_token.strip()
                else ""
            )
        else:
            resume_token = _stored_text(row, "resume_token")
        raw_outcome = _stored_text(row, "outcome")
        if raw_outcome == _ACTIVE_MARKER:
            outcome = None
        elif raw_outcome in _STORED_OUTCOMES:
            outcome = RuntimeOutcome(raw_outcome)
        else:
            raise RuntimeError("persisted runtime session outcome is not resumable")
        return RuntimeSessionRecord(
            task_id=task_id,
            runtime_id=runtime_id,
            thread_id=thread_id,
            resume_token=resume_token,
            outcome=outcome,
            updated_at=_stored_updated_at(row),
        )

    @staticmethod
    def _next_updated_at(previous: str | None) -> str:
        """Return a wall-clock timestamp that also advances the durable session epoch.

        Recovery claim identity includes ``updated_at``. A repeated resumable result may keep
        the same runtime/thread/token/outcome, so equal wall-clock readings must not collapse two
        distinct persisted epochs into the same durable recovery claim. SQLite serialization
        gives the caller one authoritative previous value; advance by one microsecond when the
        wall clock has not moved forward.
        """
        now = datetime.now(UTC)
        if previous is not None:
            if type(previous) is not str:
                raise RuntimeError(
                    "persisted runtime session updated_at has invalid SQLite storage class"
                )
            try:
                previous_value = datetime.fromisoformat(previous)
            except ValueError as exc:
                raise RuntimeError("persisted runtime session updated_at is invalid") from exc
            if previous_value.tzinfo is None or previous_value.utcoffset() is None:
                raise RuntimeError("persisted runtime session updated_at must be timezone-aware")
            if previous_value.utcoffset() != timedelta(0):
                raise RuntimeError("persisted runtime session updated_at must be UTC")
            if previous_value.astimezone(UTC).isoformat() != previous:
                raise RuntimeError("persisted runtime session updated_at is not canonical")
            if now <= previous_value:
                now = previous_value + timedelta(microseconds=1)
        return now.isoformat()

    def get(self, task_id: str) -> RuntimeSessionRecord | None:
        with self._store.connection() as conn:
            return self.get_with_connection(conn, task_id)

    def get_with_connection(
        self,
        conn: sqlite3.Connection,
        task_id: str,
    ) -> RuntimeSessionRecord | None:
        """Read one session inside a caller-owned transaction/CAS boundary."""
        lookup_task_id = _require_exact_text(
            task_id,
            field_name="task_id",
            non_empty=False,
        )
        row = conn.execute(
            """
            SELECT task_id, runtime_id, thread_id, resume_token, outcome, updated_at
            FROM runtime_sessions
            WHERE task_id = ?
            """,
            (lookup_task_id,),
        ).fetchone()
        if row is None:
            return None
        return self._record_from_row(row)

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
            self._record_from_row(row, recovery_inventory=True) for row in rows
        )

    def record_active(
        self,
        *,
        task_id: str,
        runtime_id: str,
        thread_id: str,
        resume_token: str,
    ) -> None:
        """Persist a new durable routing pointer; never overwrite recovery state."""
        with self._store.connection() as conn:
            self.record_active_with_connection(
                conn,
                task_id=task_id,
                runtime_id=runtime_id,
                thread_id=thread_id,
                resume_token=resume_token,
            )

    def record_active_with_connection(
        self,
        conn: sqlite3.Connection,
        *,
        task_id: str,
        runtime_id: str,
        thread_id: str,
        resume_token: str,
    ) -> None:
        """Insert ACTIVE cursor inside a caller-owned transaction.

        A duplicate task/session is a recovery fact, not something a fresh start may replace.
        SQLite uniqueness therefore deliberately raises instead of using an UPSERT.
        """
        task_id = _require_exact_text(task_id, field_name="task_id", non_empty=True)
        runtime_id = _require_exact_text(runtime_id, field_name="runtime_id", non_empty=True)
        thread_id = _require_exact_text(thread_id, field_name="thread_id", non_empty=True)
        resume_token_snapshot = _resume_token_for_storage(resume_token)
        if resume_token_snapshot is None:
            raise ValueError("active runtime resume token must not be empty")
        now = self._next_updated_at(None)
        try:
            conn.execute(
                """
                INSERT INTO runtime_sessions(
                    task_id, runtime_id, thread_id, resume_token, outcome, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    runtime_id,
                    thread_id,
                    resume_token_snapshot,
                    _ACTIVE_MARKER,
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError(
                f"Task {task_id} already owns a persisted runtime session; resume it explicitly"
            ) from exc

    def record_result(
        self,
        *,
        task_id: str,
        runtime_id: str,
        thread_id: str,
        result: RuntimeResult,
    ) -> None:
        with self._store.connection() as conn:
            self.record_result_with_connection(
                conn,
                task_id=task_id,
                runtime_id=runtime_id,
                thread_id=thread_id,
                result=result,
            )

    def record_result_with_connection(
        self,
        conn: sqlite3.Connection,
        *,
        task_id: str,
        runtime_id: str,
        thread_id: str,
        result: RuntimeResult,
    ) -> None:
        """Persist the resumable result cursor in a caller-owned transaction."""
        task_id = _require_exact_text(task_id, field_name="task_id", non_empty=True)
        runtime_id = _require_exact_text(runtime_id, field_name="runtime_id", non_empty=True)
        thread_id = _require_exact_text(thread_id, field_name="thread_id", non_empty=True)
        outcome, resume_token = _result_for_storage(result)
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        existing_row = conn.execute(
            """
            SELECT task_id, runtime_id, thread_id, resume_token, outcome, updated_at
            FROM runtime_sessions
            WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()
        existing = self._record_from_row(existing_row) if existing_row is not None else None
        if existing is not None:
            if existing.runtime_id != runtime_id:
                raise ValueError("runtime result does not match persisted runtime session")
            if existing.thread_id != thread_id:
                raise ValueError("runtime result thread does not match persisted runtime session")

        if outcome not in _RESUMABLE_OUTCOMES:
            self.delete_with_connection(conn, task_id)
            return
        resume_token_snapshot = _resume_token_for_storage(resume_token)
        if resume_token_snapshot is None:
            if outcome in {
                RuntimeOutcome.WAITING_APPROVAL,
                RuntimeOutcome.PAUSED,
            }:
                raise ValueError("resumable runtime outcome requires a usable resume token")
            self.delete_with_connection(conn, task_id)
            return

        previous = existing.updated_at if existing is not None else None
        now = self._next_updated_at(previous)
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
                resume_token_snapshot,
                outcome.value,
                now,
            ),
        )

    def delete(self, task_id: str) -> None:
        with self._store.connection() as conn:
            self.delete_with_connection(conn, task_id)

    def delete_with_connection(self, conn: sqlite3.Connection, task_id: str) -> None:
        """Delete a recovery cursor inside a caller-owned transaction."""
        lookup_task_id = _require_exact_text(
            task_id,
            field_name="task_id",
            non_empty=False,
        )
        conn.execute("DELETE FROM runtime_sessions WHERE task_id = ?", (lookup_task_id,))
