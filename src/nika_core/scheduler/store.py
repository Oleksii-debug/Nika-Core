from __future__ import annotations

import json
import math
import sqlite3
from datetime import UTC, datetime
from typing import Any

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_state import TaskState
from nika_core.scheduler.contracts import ScheduledJob, TriggerKind

IMMUTABLE_JOB_BINDING_KEY = "_nika_immutable_job_binding_v1"


class ScheduledJobStore:
    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def upsert(self, job: ScheduledJob) -> None:
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self.upsert_with_connection(conn, job)

    def upsert_with_connection(self, conn: sqlite3.Connection, job: ScheduledJob) -> None:
        """Upsert one job inside a caller-owned SQLite transaction."""
        trigger, payload = _validated_job_data(job)
        now = datetime.now(UTC).isoformat()
        existing = conn.execute(
            "SELECT created_at, payload_json FROM scheduled_jobs WHERE job_id = ?",
            (job.job_id,),
        ).fetchone()
        if existing is not None:
            incoming_binding = payload.get(IMMUTABLE_JOB_BINDING_KEY)
            if incoming_binding is not None:
                existing_payload = json.loads(existing["payload_json"])
                existing_binding = existing_payload.get(IMMUTABLE_JOB_BINDING_KEY)
                if existing_binding is not None and existing_binding != incoming_binding:
                    raise ValueError("scheduled job immutable binding conflict")
        created_at = existing["created_at"] if existing else now
        conn.execute(
            """INSERT INTO scheduled_jobs(
                job_id, action_id, trigger_kind, trigger_json, payload_json, enabled,
                coalesce, max_instances, misfire_grace_seconds, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                action_id = excluded.action_id,
                trigger_kind = excluded.trigger_kind,
                trigger_json = excluded.trigger_json,
                payload_json = excluded.payload_json,
                enabled = excluded.enabled,
                coalesce = excluded.coalesce,
                max_instances = excluded.max_instances,
                misfire_grace_seconds = excluded.misfire_grace_seconds,
                updated_at = excluded.updated_at
            """,
            (
                job.job_id,
                job.action_id,
                job.trigger_kind.value,
                json.dumps(trigger, sort_keys=True, separators=(",", ":"), allow_nan=False),
                json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False),
                int(job.enabled),
                int(job.coalesce),
                job.max_instances,
                job.misfire_grace_seconds,
                created_at,
                now,
            ),
        )

    def get(self, job_id: str) -> ScheduledJob | None:
        job_key = _exact_text(job_id, "job_id")
        with self._store.connection() as conn:
            return self.get_with_connection(conn, job_key)

    def get_with_connection(
        self,
        conn: sqlite3.Connection,
        job_id: str,
    ) -> ScheduledJob | None:
        """Read one job inside a caller-owned SQLite transaction."""
        job_key = _exact_text(job_id, "job_id")
        row = conn.execute(
            "SELECT * FROM scheduled_jobs WHERE job_id = ?",
            (job_key,),
        ).fetchone()
        return _from_row(row) if row else None

    def list_enabled(self) -> tuple[ScheduledJob, ...]:
        with self._store.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM scheduled_jobs WHERE enabled = 1 ORDER BY job_id"
            ).fetchall()
        return tuple(_from_row(row) for row in rows)

    def set_enabled(self, job_id: str, enabled: bool) -> bool:
        with self._store.connection() as conn:
            return self.set_enabled_with_connection(conn, job_id, enabled)

    def set_enabled_with_connection(
        self,
        conn: sqlite3.Connection,
        job_id: str,
        enabled: bool,
    ) -> bool:
        """Set durable enabled state inside a caller-owned SQLite transaction."""
        job_key = _exact_text(job_id, "job_id")
        if type(enabled) is not bool:
            raise TypeError("enabled must be an exact bool")
        cursor = conn.execute(
            "UPDATE scheduled_jobs SET enabled = ?, updated_at = ? WHERE job_id = ?",
            (int(enabled), datetime.now(UTC).isoformat(), job_key),
        )
        return cursor.rowcount > 0

    def delete(self, job_id: str) -> bool:
        job_key = _exact_text(job_id, "job_id")
        with self._store.connection() as conn:
            cursor = conn.execute(
                "DELETE FROM scheduled_jobs WHERE job_id = ?",
                (job_key,),
            )
        return cursor.rowcount > 0

    def task_state(self, task_id: str) -> TaskState | None:
        """Read canonical task authority for scheduler dispatch without mutating it."""
        task_key = _exact_text(task_id, "task_id")
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT state FROM tasks WHERE task_id = ?",
                (task_key,),
            ).fetchone()
        if row is None:
            return None
        return TaskState(str(row["state"]))


def _validated_job_data(job: ScheduledJob) -> tuple[dict[str, Any], dict[str, Any]]:
    if type(job) is not ScheduledJob:
        raise TypeError("job must be an exact ScheduledJob")
    _nonempty_text(job.job_id, "job_id")
    _nonempty_text(job.action_id, "action_id")
    if type(job.trigger_kind) is not TriggerKind:
        raise TypeError("trigger_kind must be an exact TriggerKind")
    if type(job.enabled) is not bool or type(job.coalesce) is not bool:
        raise TypeError("enabled and coalesce must be exact bool values")
    if type(job.max_instances) is not int or job.max_instances <= 0:
        raise ValueError("max_instances must be a positive exact integer")
    if job.misfire_grace_seconds is not None and (
        type(job.misfire_grace_seconds) is not int
        or job.misfire_grace_seconds <= 0
    ):
        raise ValueError(
            "misfire_grace_seconds must be a positive exact integer or None"
        )
    trigger = _canonical_json_object(job.trigger, "trigger")
    if not trigger:
        raise ValueError("trigger configuration must not be empty")
    payload = _canonical_json_object(job.payload, "payload")
    immutable_binding = payload.get(IMMUTABLE_JOB_BINDING_KEY)
    if immutable_binding is not None and (
        type(immutable_binding) is not str or not immutable_binding.strip()
    ):
        raise ValueError("scheduled job immutable binding must be a non-empty string")
    return trigger, payload


def _canonical_json_object(value: object, label: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise TypeError(f"{label} must be an exact dict")
    return {
        key: _canonical_json_value(item, label)
        for key, item in value.items()
        if _exact_json_key(key, label)
    }


def _exact_json_key(key: object, label: str) -> bool:
    if type(key) is not str:
        raise TypeError(f"{label} keys must be exact strings")
    return True


def _canonical_json_value(value: object, label: str) -> Any:
    if value is None or type(value) is bool or type(value) is int or type(value) is str:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{label} contains a non-finite number")
        return value
    if type(value) is list:
        return [_canonical_json_value(item, label) for item in value]
    if type(value) is dict:
        return _canonical_json_object(value, label)
    raise TypeError(f"{label} must contain only exact JSON-compatible values")


def _exact_text(value: object, label: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{label} must be an exact string")
    return value


def _nonempty_text(value: object, label: str) -> str:
    text = _exact_text(value, label)
    if not text.strip():
        raise ValueError(f"{label} must not be empty")
    return text


def _from_row(row: object) -> ScheduledJob:
    return ScheduledJob(
        job_id=row["job_id"],
        action_id=row["action_id"],
        trigger_kind=TriggerKind(row["trigger_kind"]),
        trigger=json.loads(row["trigger_json"]),
        payload=json.loads(row["payload_json"]),
        enabled=bool(row["enabled"]),
        coalesce=bool(row["coalesce"]),
        max_instances=int(row["max_instances"]),
        misfire_grace_seconds=row["misfire_grace_seconds"],
    )
