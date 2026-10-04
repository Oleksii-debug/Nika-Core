from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.memory.contracts import MemoryRecord, MemoryScope


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
        owner_id = _required("owner_id", owner_id)
        namespace = _required("namespace", namespace)
        key = _required("key", key)
        if type(user_approved) is not bool:
            raise ValueError("user_approved must be a boolean")
        if scope is MemoryScope.USER and not user_approved:
            raise PermissionError("user long-term memory requires explicit approval")
        if expires_at is not None:
            expires_at = _as_utc(expires_at)
        now = datetime.now(UTC)
        body = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        with self._store.connection() as conn:
            existing = conn.execute(
                "SELECT created_at FROM memory_records WHERE scope = ? AND owner_id = ? "
                "AND namespace = ? AND memory_key = ?",
                (scope.value, owner_id, namespace, key),
            ).fetchone()
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
        current = _as_utc(now) if now is not None else datetime.now(UTC)
        with self._store.connection() as conn:
            # A namespace read must not delete another owner's or scope's memory.
            # Keep expiry cleanup and the returned snapshot in one transaction.
            conn.execute(
                "DELETE FROM memory_records WHERE scope = ? AND owner_id = ? "
                "AND namespace = ? AND expires_at IS NOT NULL AND expires_at <= ?",
                (scope.value, owner_id, namespace, current.isoformat()),
            )
            rows = conn.execute(
                "SELECT * FROM memory_records WHERE scope = ? AND owner_id = ? "
                "AND namespace = ? ORDER BY memory_key",
                (scope.value, owner_id, namespace),
            ).fetchall()
            # Decode before commit, so corrupt rows roll back scoped expiry cleanup.
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


def _reject_memory_constant(value: str) -> None:
    raise ValueError(f"invalid stored memory JSON constant: {value}")


def _unique_memory_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate stored memory JSON object key")
        result[key] = value
    return result


def _validate_scalar_unicode(value: Any) -> None:
    # json.loads accepts lone escaped surrogates, which break UTF-8 consumers.
    pending: list[Any] = [value]
    while pending:
        item = pending.pop()
        if type(item) is str:
            if any(0xD800 <= ord(char) <= 0xDFFF for char in item):
                raise ValueError("stored memory JSON contains invalid Unicode")
        elif type(item) is list:
            pending.extend(item)
        elif type(item) is dict:
            pending.extend(item)
            pending.extend(item.values())


def _record_from_row(row: Any) -> MemoryRecord:
    scope = MemoryScope(row["scope"])
    approval = row["user_approved"]
    if type(approval) is not int or approval not in (0, 1):
        raise ValueError("invalid stored memory approval flag")
    if scope is MemoryScope.USER and approval != 1:
        raise ValueError("user memory lacks durable explicit approval")
    body = row["value_json"]
    if type(body) is not str:
        raise ValueError("stored memory JSON must be text")
    value = json.loads(
        body, parse_constant=_reject_memory_constant, object_pairs_hook=_unique_memory_pairs
    )
    _validate_scalar_unicode(value)
    return MemoryRecord(
        scope=scope,
        owner_id=row["owner_id"],
        namespace=row["namespace"],
        key=row["memory_key"],
        value=value,
        user_approved=bool(approval),
        expires_at=_parse_optional(row["expires_at"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )
