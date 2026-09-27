from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from nika_core.data.sqlite import SQLiteStore


class IdempotencyStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    operation_key: str
    task_id: str
    operation_type: str
    input_fingerprint: str
    status: IdempotencyStatus
    result: Mapping[str, Any] | None
    created_at: str
    updated_at: str


class IdempotencyConflictError(RuntimeError):
    pass


def _require_exact_text(value: object, *, field_name: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{field_name} must be exact text")
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty")
    return value


def _stored_text(row: sqlite3.Row, field_name: str) -> str:
    value = row[field_name]
    if type(value) is not str:
        raise RuntimeError(
            f"persisted idempotency field {field_name} has invalid SQLite storage class"
        )
    if not value.strip():
        raise RuntimeError(f"persisted idempotency field {field_name} is empty")
    return value


def _stored_timestamp(row: sqlite3.Row, field_name: str) -> str:
    value = _stored_text(row, field_name)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise RuntimeError(f"persisted idempotency {field_name} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError(f"persisted idempotency {field_name} must be timezone-aware")
    if parsed.utcoffset() != timedelta(0):
        raise RuntimeError(f"persisted idempotency {field_name} must be UTC")
    if parsed.astimezone(UTC).isoformat() != value:
        raise RuntimeError(f"persisted idempotency {field_name} is not canonical")
    return value


def _serialize_result(result: Mapping[str, Any] | None) -> str | None:
    if result is None:
        return None
    if not isinstance(result, Mapping):
        raise TypeError("idempotency result must be a mapping when provided")
    try:
        return json.dumps(dict(result), ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("idempotency result must be JSON serializable") from exc


def _stored_result(row: sqlite3.Row) -> Mapping[str, Any] | None:
    raw = row["result_json"]
    if raw is None:
        return None
    if type(raw) is not str:
        raise RuntimeError(
            "persisted idempotency field result_json has invalid SQLite storage class"
        )
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("persisted idempotency result_json is invalid") from exc
    if type(decoded) is not dict:
        raise RuntimeError("persisted idempotency result_json must be an object")
    if _serialize_result(decoded) != raw:
        raise RuntimeError("persisted idempotency result_json is not canonical")
    return decoded


class IdempotencyLedger:
    """Fail-closed ledger for external side effects that may be replayed after restart."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def reserve(
        self,
        *,
        operation_key: str,
        task_id: str,
        operation_type: str,
        input_fingerprint: str,
    ) -> IdempotencyRecord:
        record, _ = self.reserve_once(
            operation_key=operation_key,
            task_id=task_id,
            operation_type=operation_type,
            input_fingerprint=input_fingerprint,
        )
        return record

    def reserve_once(
        self,
        *,
        operation_key: str,
        task_id: str,
        operation_type: str,
        input_fingerprint: str,
    ) -> tuple[IdempotencyRecord, bool]:
        """Reserve an operation and report whether this call created the reservation.

        The boolean is important at side-effect boundaries: a caller must never replay an
        already-PENDING or UNCERTAIN operation merely because ``reserve`` returned the existing
        record. Concurrent callers therefore get one durable winner and one non-created result.
        """
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self.reserve_with_connection(
                conn,
                operation_key=operation_key,
                task_id=task_id,
                operation_type=operation_type,
                input_fingerprint=input_fingerprint,
            )

    def reserve_with_connection(
        self,
        conn: sqlite3.Connection,
        *,
        operation_key: str,
        task_id: str,
        operation_type: str,
        input_fingerprint: str,
    ) -> tuple[IdempotencyRecord, bool]:
        """Reserve a side effect inside a caller-owned transaction."""
        operation_key = _require_exact_text(operation_key, field_name="operation_key")
        task_id = _require_exact_text(task_id, field_name="task_id")
        operation_type = _require_exact_text(operation_type, field_name="operation_type")
        input_fingerprint = _require_exact_text(
            input_fingerprint,
            field_name="input_fingerprint",
        )

        existing = conn.execute(
            "SELECT * FROM idempotency_records WHERE operation_key = ?",
            (operation_key,),
        ).fetchone()
        if existing is not None:
            record = self._from_row(existing)
            if (
                record.task_id != task_id
                or record.operation_type != operation_type
                or record.input_fingerprint != input_fingerprint
            ):
                raise IdempotencyConflictError(
                    "operation_key already belongs to different operation input"
                )
            return record, False

        now = datetime.now(UTC).isoformat()
        conn.execute(
            """
            INSERT INTO idempotency_records(
                operation_key, task_id, operation_type, input_fingerprint,
                status, result_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                operation_key,
                task_id,
                operation_type,
                input_fingerprint,
                IdempotencyStatus.PENDING.value,
                now,
                now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM idempotency_records WHERE operation_key = ?",
            (operation_key,),
        ).fetchone()
        if row is None:  # pragma: no cover - SQLite insert/select invariant
            raise RuntimeError("idempotency reservation disappeared inside transaction")
        return self._from_row(row), True

    def complete(
        self,
        operation_key: str,
        result: Mapping[str, Any] | None = None,
    ) -> IdempotencyRecord:
        return self._set_status(operation_key, IdempotencyStatus.COMPLETED, result)

    def complete_with_connection(
        self,
        conn: sqlite3.Connection,
        operation_key: str,
        result: Mapping[str, Any] | None = None,
    ) -> IdempotencyRecord:
        return self._set_status_with_connection(
            conn,
            operation_key,
            IdempotencyStatus.COMPLETED,
            result,
        )

    def mark_uncertain(self, operation_key: str) -> IdempotencyRecord:
        """Mark an interrupted side effect as unsafe to replay without reconciliation."""
        return self._set_status(operation_key, IdempotencyStatus.UNCERTAIN, None)

    def mark_uncertain_with_connection(
        self,
        conn: sqlite3.Connection,
        operation_key: str,
    ) -> IdempotencyRecord:
        return self._set_status_with_connection(
            conn,
            operation_key,
            IdempotencyStatus.UNCERTAIN,
            None,
        )

    def release_pending(self, operation_key: str) -> None:
        """Forget a proven-not-applied side effect so a later explicit attempt may retry."""
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self.release_pending_with_connection(conn, operation_key)

    def release_pending_with_connection(
        self,
        conn: sqlite3.Connection,
        operation_key: str,
    ) -> None:
        current = self._require_with_connection(conn, operation_key)
        if current.status != IdempotencyStatus.PENDING:
            raise IdempotencyConflictError("only pending operations may be released")
        conn.execute(
            "DELETE FROM idempotency_records WHERE operation_key = ?",
            (operation_key,),
        )

    def reconcile_completed(
        self,
        operation_key: str,
        result: Mapping[str, Any] | None = None,
    ) -> IdempotencyRecord:
        """Close an UNCERTAIN record only after an external system proves completion."""
        operation_key = _require_exact_text(operation_key, field_name="operation_key")
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = self._require_with_connection(conn, operation_key)
            if current.status is not IdempotencyStatus.UNCERTAIN:
                raise IdempotencyConflictError(
                    "only uncertain operations require reconciliation"
                )
            return self._set_status_with_connection(
                conn,
                operation_key,
                IdempotencyStatus.COMPLETED,
                result,
                allow_uncertain_completion=True,
            )

    def get(self, operation_key: str) -> IdempotencyRecord | None:
        operation_key = _require_exact_text(operation_key, field_name="operation_key")
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT * FROM idempotency_records WHERE operation_key = ?",
                (operation_key,),
            ).fetchone()
        return None if row is None else self._from_row(row)

    def require(self, operation_key: str) -> IdempotencyRecord:
        record = self.get(operation_key)
        if record is None:
            raise KeyError(f"Unknown idempotency operation: {operation_key}")
        return record

    def list_for_task(
        self,
        task_id: str,
        *,
        status: IdempotencyStatus | None = None,
    ) -> tuple[IdempotencyRecord, ...]:
        """Return a stable inventory of side-effect records owned by one Nika task."""
        task_id = _require_exact_text(task_id, field_name="task_id")
        if status is not None and type(status) is not IdempotencyStatus:
            raise TypeError("status must be an IdempotencyStatus when provided")
        query = "SELECT * FROM idempotency_records WHERE task_id = ?"
        params: tuple[object, ...] = (task_id,)
        if status is not None:
            query += " AND status = ?"
            params += (status.value,)
        query += " ORDER BY created_at, operation_key"
        with self._store.connection() as conn:
            rows = conn.execute(query, params).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def list_uncertain_for_task(self, task_id: str) -> tuple[IdempotencyRecord, ...]:
        """Return external operations that must be reconciled before automatic recovery."""
        return self.list_for_task(task_id, status=IdempotencyStatus.UNCERTAIN)

    def promote_pending_to_uncertain(self, task_id: str) -> tuple[str, ...]:
        """Close the process-loss ambiguity window for one startup recovery task.

        ``PENDING`` means the owning process may still be between durable reservation and
        finalization, so normal execution must never rewrite it.  Startup recovery runs only
        after that process has been recreated; at that boundary every leftover reservation has
        an unknown external outcome and is therefore durably promoted to ``UNCERTAIN``.
        """
        task_id = _require_exact_text(task_id, field_name="task_id")
        now = datetime.now(UTC).isoformat()
        with self._store.connection() as conn:
            rows = conn.execute(
                """
                UPDATE idempotency_records
                SET status = ?, result_json = NULL, updated_at = ?
                WHERE task_id = ? AND status = ?
                RETURNING operation_key
                """,
                (
                    IdempotencyStatus.UNCERTAIN.value,
                    now,
                    task_id,
                    IdempotencyStatus.PENDING.value,
                ),
            ).fetchall()
        return tuple(sorted(row["operation_key"] for row in rows))

    def _set_status(
        self,
        operation_key: str,
        status: IdempotencyStatus,
        result: Mapping[str, Any] | None,
        *,
        allow_uncertain_completion: bool = False,
    ) -> IdempotencyRecord:
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self._set_status_with_connection(
                conn,
                operation_key,
                status,
                result,
                allow_uncertain_completion=allow_uncertain_completion,
            )

    def _set_status_with_connection(
        self,
        conn: sqlite3.Connection,
        operation_key: str,
        status: IdempotencyStatus,
        result: Mapping[str, Any] | None,
        *,
        allow_uncertain_completion: bool = False,
    ) -> IdempotencyRecord:
        operation_key = _require_exact_text(operation_key, field_name="operation_key")
        if type(status) is not IdempotencyStatus:
            raise TypeError("status must be an IdempotencyStatus")
        current = self._require_with_connection(conn, operation_key)
        result_json = _serialize_result(result)
        if current.status is IdempotencyStatus.COMPLETED:
            if status is not IdempotencyStatus.COMPLETED:
                raise IdempotencyConflictError("completed operation cannot be reopened")
            current_result_json = _serialize_result(current.result)
            if result_json != current_result_json:
                raise IdempotencyConflictError("completed operation result is immutable")
            return current
        if (
            current.status is IdempotencyStatus.UNCERTAIN
            and status is IdempotencyStatus.COMPLETED
            and not allow_uncertain_completion
        ):
            raise IdempotencyConflictError(
                "uncertain operation requires external reconciliation before completion"
            )
        now = datetime.now(UTC).isoformat()
        conn.execute(
            """
            UPDATE idempotency_records
            SET status = ?, result_json = ?, updated_at = ?
            WHERE operation_key = ?
            """,
            (status.value, result_json, now, operation_key),
        )
        return self._require_with_connection(conn, operation_key)

    @staticmethod
    def _require_with_connection(
        conn: sqlite3.Connection,
        operation_key: str,
    ) -> IdempotencyRecord:
        operation_key = _require_exact_text(operation_key, field_name="operation_key")
        row = conn.execute(
            "SELECT * FROM idempotency_records WHERE operation_key = ?",
            (operation_key,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown idempotency operation: {operation_key}")
        return IdempotencyLedger._from_row(row)

    @staticmethod
    def _from_row(row: sqlite3.Row) -> IdempotencyRecord:
        raw_status = _stored_text(row, "status")
        try:
            status = IdempotencyStatus(raw_status)
        except ValueError as exc:
            raise RuntimeError("persisted idempotency status is unsupported") from exc
        return IdempotencyRecord(
            operation_key=_stored_text(row, "operation_key"),
            task_id=_stored_text(row, "task_id"),
            operation_type=_stored_text(row, "operation_type"),
            input_fingerprint=_stored_text(row, "input_fingerprint"),
            status=status,
            result=_stored_result(row),
            created_at=_stored_timestamp(row, "created_at"),
            updated_at=_stored_timestamp(row, "updated_at"),
        )
