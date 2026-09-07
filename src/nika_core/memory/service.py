from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.memory.contracts import MemoryConflictError, MemoryRecord, MemoryScope

_UNCONDITIONAL = object()


class MemoryService:
    def __init__(self, store: SQLiteStore, audit: AuditLog | None = None) -> None:
        self._store = store
        self._audit = audit

    def put(
        self,
        *,
        scope: MemoryScope,
        owner_id: str,
        namespace: str,
        key: str,
        value: Any,
        user_approved: bool = False,
        expires_at: datetime | None = None,
    ) -> MemoryRecord:
        """Write memory with the historical last-write-wins compatibility contract."""
        return self._put(
            scope=scope,
            owner_id=owner_id,
            namespace=namespace,
            key=key,
            value=value,
            user_approved=user_approved,
            expires_at=expires_at,
            expected_updated_at=_UNCONDITIONAL,
        )

    def compare_and_put(
        self,
        *,
        scope: MemoryScope,
        owner_id: str,
        namespace: str,
        key: str,
        value: Any,
        expected_updated_at: datetime | None,
        user_approved: bool = False,
        expires_at: datetime | None = None,
    ) -> MemoryRecord:
        """Atomically create-or-update only when the caller's durable revision is current.

        expected_updated_at=None means the caller requires the record to be absent.
        Passing a timestamp requires that exact stored revision. A mismatch fails closed.
        """
        return self._put(
            scope=scope,
            owner_id=owner_id,
            namespace=namespace,
            key=key,
            value=value,
            user_approved=user_approved,
            expires_at=expires_at,
            expected_updated_at=expected_updated_at,
        )

    def _put(
        self,
        *,
        scope: MemoryScope,
        owner_id: str,
        namespace: str,
        key: str,
        value: Any,
        user_approved: bool,
        expires_at: datetime | None,
        expected_updated_at: datetime | None | object,
    ) -> MemoryRecord:
        owner_id = _required("owner_id", owner_id)
        namespace = _required("namespace", namespace)
        key = _required("key", key)
        if scope is MemoryScope.USER and not user_approved:
            raise PermissionError("user long-term memory requires explicit approval")
        if expires_at is not None:
            expires_at = _as_utc(expires_at)
        expected = (
            _as_utc(expected_updated_at)
            if isinstance(expected_updated_at, datetime)
            else expected_updated_at
        )
        body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._store.connection() as conn:
            if expected is not _UNCONDITIONAL:
                conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT created_at, updated_at FROM memory_records "
                "WHERE scope = ? AND owner_id = ? AND namespace = ? AND memory_key = ?",
                (scope.value, owner_id, namespace, key),
            ).fetchone()
            if expected is not _UNCONDITIONAL:
                _require_expected_revision(existing, expected)
            now = _next_revision(existing["updated_at"] if existing else None)
            created_at = existing["created_at"] if existing else now.isoformat()
            conn.execute(
                """INSERT INTO memory_records(
                    scope, owner_id, namespace, memory_key, value_json, user_approved,
                    expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope, owner_id, namespace, memory_key) DO UPDATE SET
                    value_json = excluded.value_json,
                    user_approved = excluded.user_approved,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (
                    scope.value,
                    owner_id,
                    namespace,
                    key,
                    body,
                    int(user_approved),
                    expires_at.isoformat() if expires_at else None,
                    created_at,
                    now.isoformat(),
                ),
            )
            if self._audit is not None:
                self._audit.append_with_connection(
                    conn,
                    event_type="memory.upserted",
                    entity_type="memory",
                    entity_id=f"{scope.value}:{owner_id}:{namespace}:{key}",
                    payload={
                        "scope": scope.value,
                        "owner_id": owner_id,
                        "namespace": namespace,
                        "key": key,
                        "expires": expires_at is not None,
                        "user_approved": user_approved,
                        "conditional": expected is not _UNCONDITIONAL,
                    },
                )
        record = self.get(scope=scope, owner_id=owner_id, namespace=namespace, key=key)
        if record is None:
            raise RuntimeError("memory record expired during write")
        return record

    def get(
        self,
        *,
        scope: MemoryScope,
        owner_id: str,
        namespace: str,
        key: str,
        now: datetime | None = None,
    ) -> MemoryRecord | None:
        current = _as_utc(now) if now else datetime.now(UTC)
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT * FROM memory_records WHERE scope = ? AND owner_id = ? "
                "AND namespace = ? AND memory_key = ?",
                (scope.value, owner_id, namespace, key),
            ).fetchone()
            if row is None:
                return None
            expires_at = _parse_optional(row["expires_at"])
            if expires_at is not None and expires_at <= current:
                conn.execute(
                    "DELETE FROM memory_records WHERE scope = ? AND owner_id = ? "
                    "AND namespace = ? AND memory_key = ?",
                    (scope.value, owner_id, namespace, key),
                )
                return None
        return _record_from_row(row)

    def list_namespace(
        self,
        *,
        scope: MemoryScope,
        owner_id: str,
        namespace: str,
        now: datetime | None = None,
    ) -> tuple[MemoryRecord, ...]:
        self.purge_expired(now=now)
        with self._store.connection() as conn:
            rows = conn.execute(
                "SELECT * FROM memory_records WHERE scope = ? AND owner_id = ? "
                "AND namespace = ? ORDER BY memory_key",
                (scope.value, owner_id, namespace),
            ).fetchall()
        return tuple(_record_from_row(row) for row in rows)

    def delete(self, *, scope: MemoryScope, owner_id: str, namespace: str, key: str) -> bool:
        with self._store.connection() as conn:
            cursor = conn.execute(
                "DELETE FROM memory_records WHERE scope = ? AND owner_id = ? "
                "AND namespace = ? AND memory_key = ?",
                (scope.value, owner_id, namespace, key),
            )
            deleted = cursor.rowcount > 0
            if deleted and self._audit is not None:
                self._audit.append_with_connection(
                    conn,
                    event_type="memory.deleted",
                    entity_type="memory",
                    entity_id=f"{scope.value}:{owner_id}:{namespace}:{key}",
                )
        return deleted

    def compare_and_delete(
        self,
        *,
        scope: MemoryScope,
        owner_id: str,
        namespace: str,
        key: str,
        expected_updated_at: datetime,
    ) -> bool:
        """Atomically delete only the exact durable revision observed by the caller."""
        owner_id = _required("owner_id", owner_id)
        namespace = _required("namespace", namespace)
        key = _required("key", key)
        expected = _as_utc(expected_updated_at)
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT updated_at FROM memory_records "
                "WHERE scope = ? AND owner_id = ? AND namespace = ? AND memory_key = ?",
                (scope.value, owner_id, namespace, key),
            ).fetchone()
            _require_expected_revision(existing, expected)
            cursor = conn.execute(
                "DELETE FROM memory_records WHERE scope = ? AND owner_id = ? "
                "AND namespace = ? AND memory_key = ? AND updated_at = ?",
                (scope.value, owner_id, namespace, key, expected.isoformat()),
            )
            if cursor.rowcount != 1:
                raise MemoryConflictError("memory record revision changed before delete")
            if self._audit is not None:
                self._audit.append_with_connection(
                    conn,
                    event_type="memory.deleted",
                    entity_type="memory",
                    entity_id=f"{scope.value}:{owner_id}:{namespace}:{key}",
                    payload={"conditional": True},
                )
        return True

    def purge_expired(self, *, now: datetime | None = None) -> int:
        current = _as_utc(now) if now else datetime.now(UTC)
        with self._store.connection() as conn:
            cursor = conn.execute(
                "DELETE FROM memory_records WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (current.isoformat(),),
            )
        return int(cursor.rowcount)


def _required(name: str, value: str) -> str:
    result = value.strip()
    if not result:
        raise ValueError(f"{name} must not be empty")
    return result


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


def _parse_optional(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _next_revision(existing: str | None) -> datetime:
    now = datetime.now(UTC)
    if existing is None:
        return now
    previous = datetime.fromisoformat(existing).astimezone(UTC)
    return max(now, previous + timedelta(microseconds=1))


def _require_expected_revision(row: Any | None, expected: datetime | None | object) -> None:
    if expected is None:
        if row is not None:
            raise MemoryConflictError("memory record already exists")
        return
    if row is None:
        raise MemoryConflictError("memory record no longer exists")
    if not isinstance(expected, datetime):
        raise TypeError("expected memory revision must be a datetime or None")
    actual = datetime.fromisoformat(row["updated_at"]).astimezone(UTC)
    if actual != expected:
        raise MemoryConflictError("memory record revision changed")


def _record_from_row(row: Any) -> MemoryRecord:
    return MemoryRecord(
        scope=MemoryScope(row["scope"]),
        owner_id=row["owner_id"],
        namespace=row["namespace"],
        key=row["memory_key"],
        value=json.loads(row["value_json"]),
        user_approved=bool(row["user_approved"]),
        expires_at=_parse_optional(row["expires_at"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )
