from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime

from nika_core.data.sqlite import SQLiteStore


@dataclass(frozen=True, slots=True)
class AuditEvent:
    event_id: int
    event_type: str
    entity_type: str
    entity_id: str
    payload: dict[str, object]
    created_at: str


class AuditLog:
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
        """Append audit evidence inside a caller-owned SQLite transaction."""
        if not event_type.strip() or not entity_type.strip() or not entity_id.strip():
            raise ValueError("audit event identifiers must not be empty")
        body = json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        cursor = conn.execute(
            "INSERT INTO audit_events(event_type, entity_type, entity_id, payload_json, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (event_type, entity_type, entity_id, body, datetime.now(UTC).isoformat()),
        )
        return int(cursor.lastrowid)

    def list_for(self, *, entity_type: str, entity_id: str) -> tuple[AuditEvent, ...]:
        with self._store.connection() as conn:
            rows = conn.execute(
                "SELECT event_id, event_type, entity_type, entity_id, payload_json, created_at "
                "FROM audit_events WHERE entity_type = ? AND entity_id = ? ORDER BY event_id",
                (entity_type, entity_id),
            ).fetchall()
        return tuple(
            AuditEvent(
                event_id=int(row["event_id"]),
                event_type=row["event_type"],
                entity_type=row["entity_type"],
                entity_id=row["entity_id"],
                payload=json.loads(row["payload_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        )
