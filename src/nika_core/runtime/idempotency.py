from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
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
        if not operation_key.strip() or not task_id.strip() or not operation_type.strip():
            raise ValueError("idempotency identifiers must not be empty")
        if not input_fingerprint.strip():
            raise ValueError("input_fingerprint must not be empty")

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
        current = self.require(operation_key)
        if current.status != IdempotencyStatus.UNCERTAIN:
            raise IdempotencyConflictError("only uncertain operations require reconciliation")
        return self._set_status(
            operation_key,
            IdempotencyStatus.COMPLETED,
            result,
            allow_uncertain_completion=True,
        )

    def get(self, operation_key: str) -> IdempotencyRecord | None:
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
        if not task_id.strip():
            raise ValueError("task_id must not be empty")
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

    def _set_status(
        self,
        operation_key: str,
        status: IdempotencyStatus,
        result: Mapping[str, Any] | None,
        *,
        allow_uncertain_completion: bool = False,
    ) -> IdempotencyRecord:
        with self._store.connection() as conn:
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
        current = self._require_with_connection(conn, operation_key)
        if current.status == IdempotencyStatus.COMPLETED and status != IdempotencyStatus.COMPLETED:
            raise IdempotencyConflictError("completed operation cannot be reopened")
        if (
            current.status == IdempotencyStatus.UNCERTAIN
            and status == IdempotencyStatus.COMPLETED
            and not allow_uncertain_completion
        ):
            raise IdempotencyConflictError(
                "uncertain operation requires external reconciliation before completion"
            )
        now = datetime.now(UTC).isoformat()
        result_json = (
            json.dumps(dict(result), ensure_ascii=False, sort_keys=True)
            if result is not None
            else None
        )
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
        row = conn.execute(
            "SELECT * FROM idempotency_records WHERE operation_key = ?",
            (operation_key,),
        ).fetchone()
        if row is None:
            raise KeyError(f"Unknown idempotency operation: {operation_key}")
        return IdempotencyLedger._from_row(row)

    @staticmethod
    def _from_row(row) -> IdempotencyRecord:
        return IdempotencyRecord(
            operation_key=row["operation_key"],
            task_id=row["task_id"],
            operation_type=row["operation_type"],
            input_fingerprint=row["input_fingerprint"],
            status=IdempotencyStatus(row["status"]),
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
