from __future__ import annotations

import json
from datetime import UTC, datetime

from nika_core.data.sqlite import SQLiteStore
from nika_core.scheduler.contracts import ScheduleIdentity, ScheduledJob, TriggerKind


class ScheduledJobStore:
    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def upsert(self, job: ScheduledJob) -> None:
        _validate_job(job)
        now = _utc_now_iso()
        with self._store.connection() as conn:
            existing = conn.execute(
                """SELECT b.scope, b.owner_id, b.dedup_key, b.product_project_id
                FROM scheduled_jobs AS j
                LEFT JOIN scheduled_job_bindings AS b ON b.job_id = j.job_id
                WHERE j.job_id = ?""",
                (job.job_id,),
            ).fetchone()
            if existing is not None and existing["scope"] is not None:
                persisted = ScheduleIdentity(
                    scope=existing["scope"],
                    owner_id=existing["owner_id"],
                    dedup_key=existing["dedup_key"],
                    product_project_id=existing["product_project_id"],
                )
                if job.identity is None:
                    raise ValueError("persisted schedule identity cannot be cleared")
                if job.identity != persisted:
                    raise ValueError("persisted schedule identity cannot be changed")
            if job.identity is not None:
                duplicate = conn.execute(
                    """SELECT job_id FROM scheduled_job_bindings
                    WHERE scope = ? AND owner_id = ? AND dedup_key = ? AND job_id != ?""",
                    (
                        job.identity.scope,
                        job.identity.owner_id,
                        job.identity.dedup_key,
                        job.job_id,
                    ),
                ).fetchone()
                if duplicate is not None:
                    raise ValueError(
                        "schedule dedup key is already bound to another job for this owner"
                    )

            created_row = conn.execute(
                "SELECT created_at FROM scheduled_jobs WHERE job_id = ?",
                (job.job_id,),
            ).fetchone()
            created_at = created_row["created_at"] if created_row else now
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
                    _canonical_json(job.trigger),
                    _canonical_json(job.payload),
                    int(job.enabled),
                    int(job.coalesce),
                    job.max_instances,
                    job.misfire_grace_seconds,
                    created_at,
                    now,
                ),
            )
            if job.identity is not None:
                conn.execute(
                    """INSERT INTO scheduled_job_bindings(
                        job_id, scope, owner_id, dedup_key, product_project_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(job_id) DO NOTHING""",
                    (
                        job.job_id,
                        job.identity.scope,
                        job.identity.owner_id,
                        job.identity.dedup_key,
                        job.identity.product_project_id,
                        now,
                    ),
                )

    def get(self, job_id: str) -> ScheduledJob | None:
        with self._store.connection() as conn:
            row = conn.execute(_JOB_SELECT + " WHERE j.job_id = ?", (job_id,)).fetchone()
        return _from_row(row) if row else None

    def list_enabled(self) -> tuple[ScheduledJob, ...]:
        with self._store.connection() as conn:
            rows = conn.execute(
                _JOB_SELECT + " WHERE j.enabled = 1 ORDER BY j.job_id"
            ).fetchall()
        return tuple(_from_row(row) for row in rows)

    def list_for_owner(self, *, scope: str, owner_id: str) -> tuple[ScheduledJob, ...]:
        if not scope.strip() or not owner_id.strip():
            raise ValueError("schedule owner scope and owner_id must not be empty")
        with self._store.connection() as conn:
            rows = conn.execute(
                _JOB_SELECT
                + " WHERE b.scope = ? AND b.owner_id = ? ORDER BY b.dedup_key, j.job_id",
                (scope, owner_id),
            ).fetchall()
        return tuple(_from_row(row) for row in rows)

    def set_enabled(self, job_id: str, enabled: bool) -> bool:
        with self._store.connection() as conn:
            cursor = conn.execute(
                "UPDATE scheduled_jobs SET enabled = ?, updated_at = ? WHERE job_id = ?",
                (int(enabled), _utc_now_iso(), job_id),
            )
        return cursor.rowcount > 0

    def delete(self, job_id: str) -> bool:
        with self._store.connection() as conn:
            cursor = conn.execute("DELETE FROM scheduled_jobs WHERE job_id = ?", (job_id,))
        return cursor.rowcount > 0


_JOB_SELECT = """SELECT
    j.*,
    b.scope AS identity_scope,
    b.owner_id AS identity_owner_id,
    b.dedup_key AS identity_dedup_key,
    b.product_project_id AS identity_product_project_id
FROM scheduled_jobs AS j
LEFT JOIN scheduled_job_bindings AS b ON b.job_id = j.job_id"""


def _validate_job(job: ScheduledJob) -> None:
    if not job.job_id.strip() or not job.action_id.strip():
        raise ValueError("job_id and action_id must not be empty")
    if (
        isinstance(job.max_instances, bool)
        or not isinstance(job.max_instances, int)
        or job.max_instances <= 0
    ):
        raise ValueError("max_instances must be a positive integer")
    grace = job.misfire_grace_seconds
    if grace is not None and (
        isinstance(grace, bool) or not isinstance(grace, int) or grace <= 0
    ):
        raise ValueError("misfire_grace_seconds must be a positive integer or None")
    if not job.trigger:
        raise ValueError("trigger configuration must not be empty")
    _canonical_json(job.trigger)
    _canonical_json(job.payload)
    _validate_time_semantics(job)


def _validate_time_semantics(job: ScheduledJob) -> None:
    if job.trigger_kind is TriggerKind.DATE and "run_date" not in job.trigger:
        raise ValueError("date trigger requires run_date")
    keys = ("run_date",) if job.trigger_kind is TriggerKind.DATE else ("start_date", "end_date")
    for key in keys:
        value = job.trigger.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValueError(f"{key} must be a timezone-aware ISO-8601 string")
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{key} must be a valid ISO-8601 datetime") from exc
        if parsed.tzinfo is None:
            raise ValueError(f"{key} must be timezone-aware")
    timezone_value = job.trigger.get("timezone")
    if timezone_value is not None and (
        not isinstance(timezone_value, str) or not timezone_value.strip()
    ):
        raise ValueError("trigger timezone must be a non-empty timezone name")


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError("scheduled job trigger and payload must be JSON-serializable") from exc


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _from_row(row: object) -> ScheduledJob:
    identity = None
    if row["identity_scope"] is not None:
        identity = ScheduleIdentity(
            scope=row["identity_scope"],
            owner_id=row["identity_owner_id"],
            dedup_key=row["identity_dedup_key"],
            product_project_id=row["identity_product_project_id"],
        )
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
        identity=identity,
    )
