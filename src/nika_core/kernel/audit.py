from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from nika_core.data.sqlite import SQLiteStore

_INTEGRITY_KEY = "_nika_audit_integrity"
_INTEGRITY_VERSION = "nika-audit-integrity-v1"
_EVENT_SCHEMA = "nika-audit-event-v1"
_ROOT_SHA256 = "0" * 64
_REDACTED = "[REDACTED]"
_CAMEL_ACRONYM_BOUNDARY = re.compile(r"([A-Z]+)([A-Z][a-z])")
_CAMEL_WORD_BOUNDARY = re.compile(r"([a-z0-9])([A-Z])")
_KEY_SEPARATOR = re.compile(r"[^0-9A-Za-z]+")
_URL_USERINFO = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://)[^/\s@]+@")
_URL_QUERY_SECRET = re.compile(r"(?i)([?&](?:token|auth|key|sig)=)[^&#\s]+")
_AUTHORIZATION_VALUE = re.compile(
    r"(?i)(\b(?:proxy[-_ ]*)?authorization\s*[:=]\s*)"
    r"(?:bearer|basic)?\s*(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;\r\n]+)"
)
_COOKIE_HEADER_VALUE = re.compile(r"(?i)(\b(?:set-cookie|cookie)\s*:\s*)[^\r\n]*")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:access[_-]?token|refresh[_-]?token|api[_-]?key|client[_-]?secret|"
    r"secret[_-]?key|private[_-]?key|password|passwd|pwd|"
    r"session(?:[_-]?(?:id|token))?|credential|credentials)\b\s*[:=]\s*)"
    r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^&;\s,<>]+)"
)
_BEARER_VALUE = re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]{1,}")
_SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "auth_header",
        "authorization",
        "authorization_header",
        "cookie",
        "cookie_header",
        "cookies",
        "credential",
        "credentials",
        "password",
        "passwd",
        "private_key",
        "refresh_token",
        "secret",
        "secret_key",
        "session",
        "session_token",
        "token",
    }
)
_SENSITIVE_SUFFIXES = tuple(f"_{key}" for key in sorted(_SENSITIVE_KEYS))


class AuditIntegrityError(RuntimeError):
    """Raised when persisted audit evidence is malformed or tampered with."""


@dataclass(frozen=True, slots=True)
class AuditIntegrityReport:
    event_count: int
    sealed_event_count: int
    legacy_event_count: int
    integrity_active: bool
    head_event_id: int | None
    head_sha256: str | None


@dataclass(frozen=True, slots=True)
class AuditEvent:
    event_id: int
    event_type: str
    entity_type: str
    entity_id: str
    payload: dict[str, object]
    created_at: str


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _normalized_key(key: str) -> str:
    separated = _CAMEL_ACRONYM_BOUNDARY.sub(r"\1_\2", key)
    separated = _CAMEL_WORD_BOUNDARY.sub(r"\1_\2", separated)
    return _KEY_SEPARATOR.sub("_", separated).strip("_").casefold()


def _is_sensitive_key(key: str) -> bool:
    normalized = _normalized_key(key)
    return normalized in _SENSITIVE_KEYS or normalized.endswith(_SENSITIVE_SUFFIXES)


def _redact_string(value: str) -> str:
    """Remove credential material embedded in otherwise benign diagnostic text."""
    redacted = _URL_USERINFO.sub(r"\1[REDACTED]@", value)
    redacted = _URL_QUERY_SECRET.sub(r"\1[REDACTED]", redacted)
    redacted = _AUTHORIZATION_VALUE.sub(r"\1[REDACTED]", redacted)
    redacted = _COOKIE_HEADER_VALUE.sub(r"\1[REDACTED]", redacted)
    redacted = _SECRET_ASSIGNMENT.sub(r"\1[REDACTED]", redacted)
    return _BEARER_VALUE.sub(r"\1[REDACTED]", redacted)


def _redact(value: object) -> object:
    if isinstance(value, Mapping):
        redacted: dict[str, object] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise TypeError("audit payload object keys must be strings")
            redacted[key] = _REDACTED if _is_sensitive_key(key) else _redact(child)
        return redacted
    if isinstance(value, list):
        return [_redact(child) for child in value]
    if isinstance(value, tuple):
        return [_redact(child) for child in value]
    if isinstance(value, str):
        return _redact_string(value)
    return value


def _payload_without_integrity(
    raw_payload: object,
    *,
    event_id: int,
) -> tuple[dict[str, object], object | None]:
    if not isinstance(raw_payload, dict):
        raise AuditIntegrityError(f"audit event {event_id} payload must be a JSON object")
    payload = dict(raw_payload)
    envelope = payload.pop(_INTEGRITY_KEY, None)
    return payload, envelope


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )


def _event_sha256(
    *,
    previous_sha256: str,
    event_id: int,
    event_type: str,
    entity_type: str,
    entity_id: str,
    payload: dict[str, object],
    created_at: str,
) -> str:
    framed = {
        "schema": _EVENT_SCHEMA,
        "previous_sha256": previous_sha256,
        "event_id": event_id,
        "event_type": event_type,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "payload": payload,
        "created_at": created_at,
    }
    return hashlib.sha256(_canonical_json(framed).encode("utf-8")).hexdigest()


def _reject_nonfinite_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON constant {value} is not allowed")


def _decode_payload_json(payload_json: object, *, event_id: int) -> dict[str, object]:
    if not isinstance(payload_json, str):
        raise AuditIntegrityError(f"audit event {event_id} payload storage is not text")
    try:
        decoded = json.loads(
            payload_json,
            parse_constant=_reject_nonfinite_json_constant,
        )
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise AuditIntegrityError(f"audit event {event_id} payload is not valid JSON") from exc
    if not isinstance(decoded, dict):
        raise AuditIntegrityError(f"audit event {event_id} payload must be a JSON object")
    return decoded


def _audit_sequence(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = 'audit_events'"
    ).fetchone()
    if row is None:
        return 0
    raw_sequence = row[0]
    if not isinstance(raw_sequence, int) or raw_sequence < 0:
        raise AuditIntegrityError("audit event sequence is invalid")
    return raw_sequence


def _verify_rows(rows: list[sqlite3.Row | tuple[Any, ...]]) -> AuditIntegrityReport:
    previous_sha256 = _ROOT_SHA256
    sealed_event_count = 0
    legacy_event_count = 0
    integrity_active = False
    head_event_id: int | None = None

    for expected_event_id, row in enumerate(rows, start=1):
        event_id = int(row[0])
        if event_id != expected_event_id:
            raise AuditIntegrityError(
                f"audit event sequence is not contiguous at event {event_id}; "
                f"expected {expected_event_id}"
            )

        event_type = str(row[1])
        entity_type = str(row[2])
        entity_id = str(row[3])
        raw_payload = _decode_payload_json(row[4], event_id=event_id)
        payload, envelope = _payload_without_integrity(raw_payload, event_id=event_id)
        created_at = str(row[5])

        expected_sha256 = _event_sha256(
            previous_sha256=previous_sha256,
            event_id=event_id,
            event_type=event_type,
            entity_type=entity_type,
            entity_id=entity_id,
            payload=payload,
            created_at=created_at,
        )

        if envelope is None:
            if integrity_active:
                raise AuditIntegrityError(
                    f"audit event {event_id} is unsealed after integrity activation"
                )
            legacy_event_count += 1
        else:
            if not isinstance(envelope, dict):
                raise AuditIntegrityError(
                    f"audit event {event_id} integrity envelope must be an object"
                )
            required = {"version", "previous_sha256", "event_sha256"}
            if set(envelope) != required:
                raise AuditIntegrityError(
                    f"audit event {event_id} integrity envelope has unexpected fields"
                )
            if envelope["version"] != _INTEGRITY_VERSION:
                raise AuditIntegrityError(
                    f"audit event {event_id} integrity version is unsupported"
                )
            persisted_previous = envelope["previous_sha256"]
            persisted_sha256 = envelope["event_sha256"]
            if not _is_sha256(persisted_previous) or not _is_sha256(persisted_sha256):
                raise AuditIntegrityError(
                    f"audit event {event_id} integrity digest is malformed"
                )
            if not hmac.compare_digest(persisted_previous, previous_sha256):
                raise AuditIntegrityError(
                    f"audit event {event_id} previous digest does not match"
                )
            if not hmac.compare_digest(persisted_sha256, expected_sha256):
                raise AuditIntegrityError(f"audit event {event_id} digest does not match")
            sealed_event_count += 1
            integrity_active = True

        previous_sha256 = expected_sha256
        head_event_id = event_id

    return AuditIntegrityReport(
        event_count=len(rows),
        sealed_event_count=sealed_event_count,
        legacy_event_count=legacy_event_count,
        integrity_active=integrity_active,
        head_event_id=head_event_id,
        head_sha256=previous_sha256 if rows else None,
    )


def _verify_connection(conn: sqlite3.Connection) -> AuditIntegrityReport:
    rows = conn.execute(
        "SELECT event_id, event_type, entity_type, entity_id, payload_json, created_at "
        "FROM audit_events ORDER BY event_id"
    ).fetchall()
    sequence = _audit_sequence(conn)
    report = _verify_rows(rows)
    expected_sequence = report.head_event_id or 0
    if sequence != expected_sequence:
        raise AuditIntegrityError(
            "audit event sequence does not match persisted history; deletion is suspected"
        )
    return report


class AuditLog:
    """Append-only audit evidence with redaction and a tamper-evident SHA-256 chain.

    The chain detects accidental corruption and unauthorized edits that do not also
    recompute the complete chain. It is not an authentication signature and does not
    protect against an attacker with unrestricted database write access who can rewrite
    every event and its digests.
    """

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def append(
        self,
        *,
        event_type: str,
        entity_type: str,
        entity_id: str,
        payload: dict[str, object] | None = None,
    ) -> int:
        with self._store.connection() as conn:
            return self.append_with_connection(
                conn,
                event_type=event_type,
                entity_type=entity_type,
                entity_id=entity_id,
                payload=payload,
            )

    def append_with_connection(
        self,
        conn: sqlite3.Connection,
        *,
        event_type: str,
        entity_type: str,
        entity_id: str,
        payload: dict[str, object] | None = None,
    ) -> int:
        """Append sealed audit evidence inside a caller-owned SQLite transaction."""
        if not event_type.strip() or not entity_type.strip() or not entity_id.strip():
            raise ValueError("audit event identifiers must not be empty")
        if payload is not None and _INTEGRITY_KEY in payload:
            raise ValueError(f"{_INTEGRITY_KEY} is reserved for audit integrity metadata")

        redacted = _redact(payload or {})
        if not isinstance(redacted, dict):
            raise TypeError("audit payload must be a dictionary")
        body = _canonical_json(redacted)
        created_at = datetime.now(UTC).isoformat()

        if not conn.in_transaction:
            conn.execute("BEGIN")
        conn.execute("SAVEPOINT nika_audit_append")
        try:
            # INSERT first so SQLite acquires its writer lock before the prior chain
            # is read. Concurrent appenders therefore serialize on SQLite itself.
            cursor = conn.execute(
                "INSERT INTO audit_events("
                "event_type, entity_type, entity_id, payload_json, created_at"
                ") "
                "VALUES (?, ?, ?, ?, ?)",
                (event_type, entity_type, entity_id, body, created_at),
            )
            event_id = int(cursor.lastrowid)

            prior_rows = conn.execute(
                "SELECT event_id, event_type, entity_type, entity_id, "
                "payload_json, created_at "
                "FROM audit_events WHERE event_id < ? ORDER BY event_id",
                (event_id,),
            ).fetchall()
            report = _verify_rows(prior_rows)
            if event_id != report.event_count + 1:
                raise AuditIntegrityError(
                    f"audit event sequence has a gap before new event {event_id}"
                )
            if _audit_sequence(conn) != event_id:
                raise AuditIntegrityError("audit event sequence changed during append")

            previous_sha256 = report.head_sha256 or _ROOT_SHA256
            event_sha256 = _event_sha256(
                previous_sha256=previous_sha256,
                event_id=event_id,
                event_type=event_type,
                entity_type=entity_type,
                entity_id=entity_id,
                payload=redacted,
                created_at=created_at,
            )
            stored_payload = dict(redacted)
            stored_payload[_INTEGRITY_KEY] = {
                "version": _INTEGRITY_VERSION,
                "previous_sha256": previous_sha256,
                "event_sha256": event_sha256,
            }
            updated = conn.execute(
                "UPDATE audit_events SET payload_json = ? WHERE event_id = ?",
                (_canonical_json(stored_payload), event_id),
            )
            if updated.rowcount != 1:
                raise AuditIntegrityError(f"audit event {event_id} could not be sealed")
        except BaseException:
            conn.execute("ROLLBACK TO SAVEPOINT nika_audit_append")
            conn.execute("RELEASE SAVEPOINT nika_audit_append")
            raise
        conn.execute("RELEASE SAVEPOINT nika_audit_append")
        return event_id

    def verify_integrity(self) -> AuditIntegrityReport:
        """Verify event ordering, deletion evidence, and all active chain seals."""
        with self._store.connection() as conn:
            if not conn.in_transaction:
                conn.execute("BEGIN")
            return _verify_connection(conn)

    def list_for(self, *, entity_type: str, entity_id: str) -> tuple[AuditEvent, ...]:
        """Return entity audit history only after the global audit chain verifies."""
        with self._store.connection() as conn:
            if not conn.in_transaction:
                conn.execute("BEGIN")
            _verify_connection(conn)
            rows = conn.execute(
                "SELECT event_id, event_type, entity_type, entity_id, payload_json, created_at "
                "FROM audit_events WHERE entity_type = ? AND entity_id = ? ORDER BY event_id",
                (entity_type, entity_id),
            ).fetchall()

        events: list[AuditEvent] = []
        for row in rows:
            event_id = int(row["event_id"])
            raw_payload = _decode_payload_json(row["payload_json"], event_id=event_id)
            payload, _ = _payload_without_integrity(raw_payload, event_id=event_id)
            events.append(
                AuditEvent(
                    event_id=event_id,
                    event_type=row["event_type"],
                    entity_type=row["entity_type"],
                    entity_id=row["entity_id"],
                    payload=payload,
                    created_at=row["created_at"],
                )
            )
        return tuple(events)
