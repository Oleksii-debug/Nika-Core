from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from nika_core.data.experience_ledger_schema import EXPERIENCE_LEDGER_SCHEMA_VERSION
from nika_core.data.sqlite import SQLiteStore

_REASON_CODE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,95}$")
_EVENT_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,191}$")


class ContinuityKind(StrEnum):
    INTERNET = "internet"
    HIBERNATE = "hibernate"
    APP_RESTART = "app_restart"
    WINDOWS_AUTOSTART = "windows_autostart"
    PAUSE_REBOOT = "pause_reboot"
    CANCEL_REBOOT = "cancel_reboot"
    UNCERTAIN_EFFECT = "uncertain_effect"
    DATA_MIGRATION = "data_migration"
    RECOVERY = "recovery"


class ContinuityOutcome(StrEnum):
    WAITING = "waiting"
    RESUMED = "resumed"
    RECONCILED = "reconciled"
    BLOCKED = "blocked"
    PRESERVED = "preserved"
    COMPLETED = "completed"
    FAILED_SAFE = "failed_safe"


@dataclass(frozen=True, slots=True)
class ExperienceEvent:
    event_key: str
    task_id: str | None
    kind: ContinuityKind
    outcome: ContinuityOutcome
    reason_code: str
    occurred_at: str
    attempt: int | None = None
    delay_seconds: float | None = None
    clock_jump_seconds: float | None = None


class ExperienceConflictError(RuntimeError):
    """The same durable event key was reused for materially different evidence."""


class ExperienceLedgerUnavailableError(RuntimeError):
    """The canonical Experience Ledger schema is unavailable or unsupported."""


class ExperienceLedger:
    """Privacy-safe V0.1 continuity outcome ledger.

    The ledger deliberately stores no prompts, provider responses, URLs, headers,
    exception text, credentials, arbitrary metadata, or raw tool payloads.
    """

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    @staticmethod
    def _assert_schema_ready(conn: sqlite3.Connection) -> None:
        try:
            row = conn.execute(
                "SELECT MAX(version) AS version FROM experience_ledger_schema_migrations"
            ).fetchone()
        except sqlite3.Error as exc:
            raise ExperienceLedgerUnavailableError(
                "experience ledger schema is unavailable; initialize SQLiteStore first"
            ) from exc
        current = int(row["version"] or 0)
        if current != EXPERIENCE_LEDGER_SCHEMA_VERSION:
            raise ExperienceLedgerUnavailableError(
                "experience ledger schema "
                f"{current} does not match supported schema {EXPERIENCE_LEDGER_SCHEMA_VERSION}"
            )

    @staticmethod
    def _validate_event_key(event_key: str) -> str:
        value = event_key.strip()
        if value != event_key or not _EVENT_KEY.fullmatch(value):
            raise ValueError("event_key must be a bounded stable identifier")
        return value

    @staticmethod
    def _validate_task_id(task_id: str | None) -> str | None:
        if task_id is None:
            return None
        value = task_id.strip()
        if not value or value != task_id or len(value) > 192:
            raise ValueError("task_id must be a bounded stable identifier")
        return value

    @staticmethod
    def _validate_reason_code(reason_code: str) -> str:
        value = reason_code.strip()
        if value != reason_code or not _REASON_CODE.fullmatch(value):
            raise ValueError("reason_code must be a bounded machine-safe code")
        return value

    @staticmethod
    def _validate_optional_number(name: str, value: float | None) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"{name} must be an int or float")
        try:
            number = float(value)
        except OverflowError as exc:
            raise ValueError(f"{name} must be finite and non-negative") from exc
        if number < 0 or not math.isfinite(number):
            raise ValueError(f"{name} must be finite and non-negative")
        return number

    @staticmethod
    def _validate_attempt(attempt: int | None) -> int | None:
        if attempt is None:
            return None
        if type(attempt) is not int:
            raise TypeError("attempt must be an integer")
        if attempt < 0 or attempt > 1_000_000:
            raise ValueError("attempt must be between 0 and 1000000")
        return attempt

    @staticmethod
    def _fingerprint_payload(
        *,
        event_key: str,
        task_id: str | None,
        kind: ContinuityKind,
        outcome: ContinuityOutcome,
        reason_code: str,
        occurred_at: str,
        attempt: int | None,
        delay_seconds: float | None,
        clock_jump_seconds: float | None,
    ) -> str:
        return json.dumps(
            {
                "attempt": attempt,
                "clock_jump_seconds": clock_jump_seconds,
                "delay_seconds": delay_seconds,
                "event_key": event_key,
                "kind": kind.value,
                "occurred_at": occurred_at,
                "outcome": outcome.value,
                "reason_code": reason_code,
                "task_id": task_id,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )

    def record(
        self,
        *,
        event_key: str,
        kind: ContinuityKind,
        outcome: ContinuityOutcome,
        reason_code: str,
        task_id: str | None = None,
        occurred_at: datetime | None = None,
        attempt: int | None = None,
        delay_seconds: float | None = None,
        clock_jump_seconds: float | None = None,
    ) -> ExperienceEvent:
        event_key = self._validate_event_key(event_key)
        task_id = self._validate_task_id(task_id)
        reason_code = self._validate_reason_code(reason_code)
        attempt = self._validate_attempt(attempt)
        delay_seconds = self._validate_optional_number("delay_seconds", delay_seconds)
        clock_jump_seconds = self._validate_optional_number(
            "clock_jump_seconds", clock_jump_seconds
        )
        generated_occurrence = occurred_at is None
        when = occurred_at or datetime.now(UTC)
        if when.tzinfo is None or when.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")
        occurred_text = when.astimezone(UTC).isoformat()
        fingerprint = self._fingerprint_payload(
            event_key=event_key,
            task_id=task_id,
            kind=kind,
            outcome=outcome,
            reason_code=reason_code,
            occurred_at=occurred_text,
            attempt=attempt,
            delay_seconds=delay_seconds,
            clock_jump_seconds=clock_jump_seconds,
        )

        with self._store.connection() as conn:
            self._assert_schema_ready(conn)
            conn.execute(
                "INSERT INTO continuity_experience_events("
                "event_key, task_id, kind, outcome, reason_code, occurred_at, attempt, "
                "delay_seconds, clock_jump_seconds, fingerprint"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(event_key) DO NOTHING",
                (
                    event_key,
                    task_id,
                    kind.value,
                    outcome.value,
                    reason_code,
                    occurred_text,
                    attempt,
                    delay_seconds,
                    clock_jump_seconds,
                    fingerprint,
                ),
            )
            row = conn.execute(
                "SELECT * FROM continuity_experience_events WHERE event_key = ?",
                (event_key,),
            ).fetchone()
            if row is None:
                raise RuntimeError("continuity experience event did not persist")
            expected_fingerprint = fingerprint
            if generated_occurrence:
                expected_fingerprint = self._fingerprint_payload(
                    event_key=event_key,
                    task_id=task_id,
                    kind=kind,
                    outcome=outcome,
                    reason_code=reason_code,
                    occurred_at=row["occurred_at"],
                    attempt=attempt,
                    delay_seconds=delay_seconds,
                    clock_jump_seconds=clock_jump_seconds,
                )
            if row["fingerprint"] != expected_fingerprint:
                raise ExperienceConflictError(
                    "continuity experience event key conflicts with existing durable evidence"
                )
        return self._from_row(row)

    def get(self, event_key: str) -> ExperienceEvent | None:
        event_key = self._validate_event_key(event_key)
        with self._store.connection() as conn:
            self._assert_schema_ready(conn)
            row = conn.execute(
                "SELECT * FROM continuity_experience_events WHERE event_key = ?",
                (event_key,),
            ).fetchone()
        return None if row is None else self._from_row(row)

    def list_for_task(self, task_id: str, *, limit: int = 100) -> tuple[ExperienceEvent, ...]:
        task_id = self._validate_task_id(task_id)
        if task_id is None:
            raise ValueError("task_id is required")
        if type(limit) is not int:
            raise TypeError("limit must be an integer")
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        with self._store.connection() as conn:
            self._assert_schema_ready(conn)
            rows = conn.execute(
                "SELECT * FROM continuity_experience_events WHERE task_id = ? "
                "ORDER BY occurred_at, event_key LIMIT ?",
                (task_id, limit),
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    @classmethod
    def _from_row(cls, row) -> ExperienceEvent:  # type: ignore[no-untyped-def]
        try:
            event_key = cls._validate_event_key(row["event_key"])
            task_id = cls._validate_task_id(row["task_id"])
            kind = ContinuityKind(row["kind"])
            outcome = ContinuityOutcome(row["outcome"])
            reason_code = cls._validate_reason_code(row["reason_code"])
            occurred_at = row["occurred_at"]
            if not isinstance(occurred_at, str):
                raise TypeError("occurred_at must be text")
            when = datetime.fromisoformat(occurred_at)
            if when.tzinfo is None or when.utcoffset() is None:
                raise ValueError("occurred_at must be timezone-aware")
            if when.astimezone(UTC).isoformat() != occurred_at:
                raise ValueError("occurred_at must use canonical UTC serialization")
            attempt = cls._validate_attempt(row["attempt"])
            delay_seconds = cls._validate_optional_number(
                "delay_seconds", row["delay_seconds"]
            )
            clock_jump_seconds = cls._validate_optional_number(
                "clock_jump_seconds", row["clock_jump_seconds"]
            )
        except (TypeError, ValueError, AttributeError) as exc:
            raise ExperienceConflictError(
                "continuity experience event contains invalid durable evidence"
            ) from exc

        expected_fingerprint = cls._fingerprint_payload(
            event_key=event_key,
            task_id=task_id,
            kind=kind,
            outcome=outcome,
            reason_code=reason_code,
            occurred_at=occurred_at,
            attempt=attempt,
            delay_seconds=delay_seconds,
            clock_jump_seconds=clock_jump_seconds,
        )
        if row["fingerprint"] != expected_fingerprint:
            raise ExperienceConflictError(
                "continuity experience event durable evidence failed integrity check"
            )
        return ExperienceEvent(
            event_key=event_key,
            task_id=task_id,
            kind=kind,
            outcome=outcome,
            reason_code=reason_code,
            occurred_at=occurred_at,
            attempt=attempt,
            delay_seconds=delay_seconds,
            clock_jump_seconds=clock_jump_seconds,
        )
