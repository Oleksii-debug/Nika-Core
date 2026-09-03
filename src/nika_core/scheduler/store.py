from __future__ import annotations

import json
from datetime import UTC, datetime

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.task_state import TaskState
from nika_core.scheduler.contracts import ScheduledJob, TriggerKind


class ScheduledJobStore:
    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def upsert(self, job: ScheduledJob) -> None:
        _validate_job(job)
        now = datetime.now(UTC).isoformat()
        with self._store.connection() as conn:
            existing = conn.execute(
                "SELECT created_at FROM scheduled_jobs WHERE job_id = ?", (job.job_id,)
            ).fetchone()
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
                    json.dumps(job.trigger, sort_keys=True, separators=(",", ":")),
                    json.dumps(job.payload, sort_keys=True, separators=(",", ":")),
                    int(job.enabled),
                    int(job.coalesce),
                    job.max_instances,
                    job.misfire_grace_seconds,
                    created_at,
                    now,
                ),
            )

    def get(self, job_id: str) -> ScheduledJob | None:
        with self._store.connection() as conn:
            row = conn.execute("SELECT * FROM scheduled_jobs WHERE job_id = ?", (job_id,)).fetchone()
        return _from_row(row) if row else None

    def list_enabled(self) -> tuple[ScheduledJob, ...]:
        with self._store.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM scheduled_jobs WHERE enabled = 1 ORDER BY job_id"
            ).fetchall()
        return tuple(_from_row(row) for row in rows)

    def task_state(self, task_id: str) -> TaskState | None:
        """Return canonical durable task authority for a declared scheduler binding."""
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT state FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return TaskState(row["state"]) if row is not None else None

    def set_enabled(self, job_id: str, enabled: bool) -> bool:
        with self._store.connection() as conn:
            cursor = conn.execute(
                "UPDATE scheduled_jobs SET enabled = ?, updated_at = ? WHERE job_id = ?",
                (int(enabled), datetime.now(UTC).isoformat(), job_id),
            )
        return cursor.rowcount > 0

    def delete(self, job_id: str) -> bool:
        with self._store.connection() as conn:
            cursor = conn.execute("DELETE FROM scheduled_jobs WHERE job_id = ?", (job_id,))
        return cursor.rowcount > 0


def _validate_job(job: ScheduledJob) -> None:
    if not job.job_id.strip() or not job.action_id.strip():
        raise ValueError("job_id and action_id must not be empty")
    if job.max_instances <= 0:
        raise ValueError("max_instances must be greater than zero")
    if job.misfire_grace_seconds is not None and job.misfire_grace_seconds <= 0:
        raise ValueError("misfire_grace_seconds must be greater than zero or None")
    if not job.trigger:
        raise ValueError("trigger configuration must not be empty")


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
