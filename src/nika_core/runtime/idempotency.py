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
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field_name} must be valid UTF-8 text") from exc
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


def _select_operation_row(
    conn: sqlite3.Connection,
    operation_key: str,
) -> sqlite3.Row | None:
    rows = conn.execute(
        """
        SELECT * FROM idempotency_records
        WHERE operation_key = ?
           OR (typeof(operation_key) != 'text' AND CAST(operation_key AS TEXT) = ?)
        """,
        (operation_key, operation_key),
    ).fetchall()
    if len(rows) > 1:
        raise RuntimeError("multiple persisted idempotency operation_key storage aliases")
    return None if not rows else rows[0]


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


def _snapshot_json_value(
    value: Any,
    *,
    active_containers: set[int] | None = None,
) -> Any:
    value_type = type(value)
    if value_type not in {dict, list, tuple}:
        if isinstance(value, (dict, list, tuple)):
            raise ValueError(
                "idempotency result containers must use exact built-in types"
            )
        return value

    active_containers = active_containers if active_containers is not None else set()
    container_id = id(value)
    if container_id in active_containers:
        raise ValueError("idempotency result must not contain circular containers")
    active_containers.add(container_id)
    try:
        if value_type is dict:
            copied: dict[str, Any] = {}
            for key, item in value.items():
                if type(key) is not str:
                    raise ValueError("idempotency result object keys must be exact text")
                try:
                    key.encode("utf-8", errors="strict")
                except UnicodeEncodeError as exc:
                    raise ValueError(
                        "idempotency result object keys must be valid UTF-8 text"
                    ) from exc
                copied[key] = _snapshot_json_value(
                    item,
                    active_containers=active_containers,
                )
            return copied
        if value_type is list:
            return [
                _snapshot_json_value(item, active_containers=active_containers)
                for item in value
            ]
        return tuple(
            _snapshot_json_value(item, active_containers=active_containers)
            for item in value
        )
    finally:
        active_containers.remove(container_id)


def _serialize_result(result: Mapping[str, Any] | None) -> str | None:
    if result is None:
        return None
    if type(result) is not dict:
        raise TypeError("idempotency result must use an exact built-in dict when provided")
    payload = _snapshot_json_value(result)
    try:
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        )
        serialized.encode("utf-8", errors="strict")
        return serialized
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise ValueError(
            "idempotency result must be JSON serializable valid UTF-8"
        ) from exc


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
    try:
        canonical = _serialize_result(decoded)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("persisted idempotency result_json is invalid") from exc
    if canonical != raw:
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

        existing = _select_operation_row(conn, operation_key)
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
        row = _select_operation_row(conn, operation_key)
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
            row = _select_operation_row(conn, operation_key)
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
        with self._store.connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM idempotency_records
                WHERE task_id = ?
                   OR (typeof(task_id) != 'text' AND CAST(task_id AS TEXT) = ?)
                ORDER BY created_at, operation_key
                """,
                (task_id, task_id),
            ).fetchall()
        records = tuple(self._from_row(row) for row in rows)
        if status is None:
            return records
        return tuple(record for record in records if record.status is status)

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
            conn.execute("BEGIN IMMEDIATE")
            task_rows = conn.execute(
                """
                SELECT * FROM idempotency_records
                WHERE task_id = ?
                   OR (typeof(task_id) != 'text' AND CAST(task_id AS TEXT) = ?)
                ORDER BY created_at, operation_key
                """,
                (task_id, task_id),
            ).fetchall()
            task_records = tuple(self._from_row(row) for row in task_rows)
            records = tuple(
                record
                for record in task_records
                if record.status is IdempotencyStatus.PENDING
            )
            if not records:
                return ()

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
            promoted_keys = tuple(
                sorted(_stored_text(row, "operation_key") for row in rows)
            )
            expected_keys = tuple(sorted(record.operation_key for record in records))
            if promoted_keys != expected_keys:  # pragma: no cover - writer-lock invariant
                raise RuntimeError("pending idempotency promotion set changed inside transaction")
            return promoted_keys

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
        row = _select_operation_row(conn, operation_key)
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
