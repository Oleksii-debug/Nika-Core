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
_MAX_JSON_DEPTH = 32


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
            existing_payload = _decode_json_object(
                existing["payload_json"],
                "persisted payload",
            )
            incoming_binding = payload.get(IMMUTABLE_JOB_BINDING_KEY)
            existing_binding = _validated_binding(
                existing_payload.get(IMMUTABLE_JOB_BINDING_KEY),
                "persisted scheduled job immutable binding",
            )
            if existing_binding is not None and incoming_binding != existing_binding:
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
                _encode_json(trigger),
                _encode_json(payload),
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
        raw_state = row["state"]
        if type(raw_state) is not str:
            raise ValueError("persisted task state is corrupt")
        return TaskState(raw_state)


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
    trigger = _canonical_json_object(job.trigger, "trigger", depth=0)
    if not trigger:
        raise ValueError("trigger configuration must not be empty")
    payload = _canonical_json_object(job.payload, "payload", depth=0)
    _validated_binding(
        payload.get(IMMUTABLE_JOB_BINDING_KEY),
        "scheduled job immutable binding",
    )
    return trigger, payload


def _validated_binding(value: object, label: str) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value


def _canonical_json_object(
    value: object,
    label: str,
    *,
    depth: int,
) -> dict[str, Any]:
    if type(value) is not dict:
        raise TypeError(f"{label} must be an exact dict")
    if depth > _MAX_JSON_DEPTH:
        raise ValueError(f"{label} exceeds durable JSON nesting limit")
    detached: dict[str, Any] = {}
    for key, item in value.items():
        if type(key) is not str:
            raise TypeError(f"{label} keys must be exact strings")
        detached[key] = _canonical_json_value(
            item,
            label,
            depth=depth + 1,
        )
    return detached


def _canonical_json_value(value: object, label: str, *, depth: int) -> Any:
    if depth > _MAX_JSON_DEPTH:
        raise ValueError(f"{label} exceeds durable JSON nesting limit")
    if value is None or type(value) is bool or type(value) is int or type(value) is str:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"{label} contains a non-finite number")
        return value
    if type(value) is list:
        return [
            _canonical_json_value(item, label, depth=depth + 1)
            for item in value
        ]
    if type(value) is dict:
        return _canonical_json_object(value, label, depth=depth)
    raise TypeError(f"{label} must contain only exact JSON-compatible values")


def _encode_json(value: dict[str, Any]) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _decode_json_object(raw: object, label: str) -> dict[str, Any]:
    if type(raw) is not str:
        raise ValueError(f"{label} is corrupt")
    try:
        decoded = json.loads(
            raw,
            parse_constant=lambda _: _reject_json_constant(label),
            object_pairs_hook=lambda pairs: _strict_json_object(pairs, label),
        )
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError(f"{label} is corrupt") from exc
    try:
        return _canonical_json_object(decoded, label, depth=0)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} is corrupt") from exc


def _reject_json_constant(label: str) -> None:
    raise ValueError(f"{label} contains a non-finite number")


def _strict_json_object(
    pairs: list[tuple[str, object]],
    label: str,
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"{label} contains duplicate object keys")
        result[key] = value
    return result


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
    job_id = _nonempty_text(row["job_id"], "persisted job_id")
    action_id = _nonempty_text(row["action_id"], "persisted action_id")
    trigger_kind_raw = _exact_text(row["trigger_kind"], "persisted trigger_kind")
    try:
        trigger_kind = TriggerKind(trigger_kind_raw)
    except ValueError as exc:
        raise ValueError("persisted trigger_kind is corrupt") from exc
    enabled = _stored_bool(row["enabled"], "persisted enabled")
    coalesce = _stored_bool(row["coalesce"], "persisted coalesce")
    max_instances = _stored_positive_int(
        row["max_instances"],
        "persisted max_instances",
    )
    grace_raw = row["misfire_grace_seconds"]
    grace = (
        None
        if grace_raw is None
        else _stored_positive_int(grace_raw, "persisted misfire_grace_seconds")
    )
    return ScheduledJob(
        job_id=job_id,
        action_id=action_id,
        trigger_kind=trigger_kind,
        trigger=_decode_json_object(row["trigger_json"], "persisted trigger"),
        payload=_decode_json_object(row["payload_json"], "persisted payload"),
        enabled=enabled,
        coalesce=coalesce,
        max_instances=max_instances,
        misfire_grace_seconds=grace,
    )


def _stored_bool(value: object, label: str) -> bool:
    if type(value) is not int or value not in (0, 1):
        raise ValueError(f"{label} is corrupt")
    return value == 1


def _stored_positive_int(value: object, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{label} is corrupt")
    return value
