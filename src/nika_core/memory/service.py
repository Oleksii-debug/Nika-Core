from __future__ import annotations

import json
import math
import sqlite3
import unicodedata
from datetime import UTC, datetime, timedelta
from typing import Any

from nika_core.data.sqlite import SQLiteStore
from nika_core.kernel.audit import AuditLog
from nika_core.memory.contracts import MemoryConflictError, MemoryRecord, MemoryScope
from nika_core.memory.minimization import minimize_for_persistence

_UNCONDITIONAL = object()
_MAX_STORED_MEMORY_JSON_DEPTH = 64
_MAX_STORED_MEMORY_INTEGER_BITS = 4096
_MAX_STORED_MEMORY_INTEGER_DECIMAL_CHARS = 1234


class MemoryService:
    def __init__(self, store: SQLiteStore, audit: AuditLog | None = None) -> None:
        self._store = store
        self._audit = audit

    @property
    def sqlite_store(self) -> SQLiteStore:
        """Return the canonical SQLite authority used by this service."""
        return self._store

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
        """Atomically write only when the caller's durable revision is current."""
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
        with self._store.connection() as conn:
            # Every mutation that assigns updated_at shares one writer boundary.
            # This prevents an unconditional writer from deriving a revision from a
            # stale pre-CAS snapshot and reusing another committed revision token.
            conn.execute("BEGIN IMMEDIATE")
            return self._put_with_connection(
                conn,
                scope=scope,
                owner_id=owner_id,
                namespace=namespace,
                key=key,
                value=value,
                user_approved=user_approved,
                expires_at=expires_at,
                expected_updated_at=expected_updated_at,
            )

    def compare_and_put_with_connection(
        self,
        conn: sqlite3.Connection,
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
        """Conditionally write inside a caller-owned canonical SQLite transaction."""
        if type(conn) is not sqlite3.Connection:
            raise TypeError("conn must be an exact sqlite3.Connection")
        return self._put_with_connection(
            conn,
            scope=scope,
            owner_id=owner_id,
            namespace=namespace,
            key=key,
            value=value,
            user_approved=user_approved,
            expires_at=expires_at,
            expected_updated_at=expected_updated_at,
        )

    def _put_with_connection(
        self,
        conn: sqlite3.Connection,
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
        scope = _require_scope(scope)
        owner_id = _required("owner_id", owner_id)
        namespace = _required("namespace", namespace)
        key = _required("key", key)
        if type(user_approved) is not bool:
            raise ValueError("user_approved must be a boolean")
        if scope is MemoryScope.USER and not user_approved:
            raise PermissionError("user long-term memory requires explicit approval")
        if expires_at is not None:
            expires_at = _as_utc(expires_at)
        if expected_updated_at is _UNCONDITIONAL:
            expected: datetime | None | object = _UNCONDITIONAL
        elif expected_updated_at is None:
            expected = None
        elif type(expected_updated_at) is datetime:
            expected = _as_utc(expected_updated_at)
        else:
            raise ValueError("expected_updated_at must be a datetime or None")
        body = json.dumps(
            minimize_for_persistence(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        try:
            body.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("memory JSON contains invalid Unicode") from exc
        # Every mutation that assigns updated_at shares one writer boundary.
        # This prevents an unconditional writer from deriving a revision from a
        # stale pre-CAS snapshot and reusing another committed revision token.
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT * FROM memory_records WHERE scope = ? AND owner_id = ? "
            "AND namespace = ? AND memory_key = ?",
            (scope.value, owner_id, namespace, key),
        ).fetchone()
        existing_record = _record_from_row(existing) if existing is not None else None
        if expected is not _UNCONDITIONAL and existing_record is not None:
            expiry = existing_record.expires_at
            if expiry is not None and _as_utc(expiry) <= datetime.now(UTC):
                cursor = conn.execute(
                    "DELETE FROM memory_records WHERE scope = ? AND owner_id = ? "
                    "AND namespace = ? AND memory_key = ? AND updated_at = ? "
                    "AND expires_at = ?",
                    (
                        scope.value,
                        owner_id,
                        namespace,
                        key,
                        existing["updated_at"],
                        existing["expires_at"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise MemoryConflictError(
                        "memory record revision changed before expiry cleanup"
                    )
                existing = None
                existing_record = None
        _require_expected_revision(existing_record, expected)
        now = _next_revision(
            existing_record.updated_at if existing_record is not None else None
        )
        created_at = existing["created_at"] if existing is not None else now.isoformat()
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
        committed = conn.execute(
            "SELECT * FROM memory_records WHERE scope = ? AND owner_id = ? "
            "AND namespace = ? AND memory_key = ?",
            (scope.value, owner_id, namespace, key),
        ).fetchone()
        if committed is None:
            raise RuntimeError("memory record disappeared during write")
        committed_record = _record_from_row(committed)
        committed_expiry = committed_record.expires_at
        if (
            committed_expiry is not None
            and _as_utc(committed_expiry) <= datetime.now(UTC)
        ):
            cursor = conn.execute(
                "DELETE FROM memory_records WHERE scope = ? AND owner_id = ? "
                "AND namespace = ? AND memory_key = ? AND updated_at = ? "
                "AND expires_at = ?",
                (
                    scope.value,
                    owner_id,
                    namespace,
                    key,
                    committed["updated_at"],
                    committed["expires_at"],
                ),
            )
            if cursor.rowcount != 1:
                raise MemoryConflictError(
                    "memory record revision changed during write finalization"
                )
            committed_record = None

        if committed_record is None:
            raise RuntimeError("memory record expired during write")
        return committed_record

    def get(
        self,
        *,
        scope: MemoryScope,
        owner_id: str,
        namespace: str,
        key: str,
        now: datetime | None = None,
    ) -> MemoryRecord | None:
        scope = _require_scope(scope)
        owner_id = _required("owner_id", owner_id)
        namespace = _required("namespace", namespace)
        key = _required("key", key)
        current = _as_utc(now) if now is not None else datetime.now(UTC)
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT * FROM memory_records WHERE scope = ? AND owner_id = ? "
                "AND namespace = ? AND memory_key = ?",
                (scope.value, owner_id, namespace, key),
            ).fetchone()
            if row is None:
                return None
            record = _record_from_row(row)
            expires_at = record.expires_at
            if expires_at is not None and _as_utc(expires_at) <= current:
                conn.execute(
                    "DELETE FROM memory_records WHERE scope = ? AND owner_id = ? "
                    "AND namespace = ? AND memory_key = ? AND updated_at = ? "
                    "AND expires_at = ?",
                    (
                        scope.value,
                        owner_id,
                        namespace,
                        key,
                        row["updated_at"],
                        row["expires_at"],
                    ),
                )
                return None
        return record

    def get_with_connection(
        self,
        conn: sqlite3.Connection,
        *,
        scope: MemoryScope,
        owner_id: str,
        namespace: str,
        key: str,
        now: datetime | None = None,
    ) -> MemoryRecord | None:
        """Read one memory row inside a caller-owned canonical SQLite transaction."""
        if type(conn) is not sqlite3.Connection:
            raise TypeError("conn must be an exact sqlite3.Connection")
        scope = _require_scope(scope)
        owner_id = _required("owner_id", owner_id)
        namespace = _required("namespace", namespace)
        key = _required("key", key)
        current = _as_utc(now) if now is not None else datetime.now(UTC)
        row = conn.execute(
            "SELECT * FROM memory_records WHERE scope = ? AND owner_id = ? "
            "AND namespace = ? AND memory_key = ?",
            (scope.value, owner_id, namespace, key),
        ).fetchone()
        if row is None:
            return None
        record = _record_from_row(row)
        expires_at = record.expires_at
        if expires_at is not None and _as_utc(expires_at) <= current:
            conn.execute(
                "DELETE FROM memory_records WHERE scope = ? AND owner_id = ? "
                "AND namespace = ? AND memory_key = ? AND updated_at = ? "
                "AND expires_at = ?",
                (
                    scope.value,
                    owner_id,
                    namespace,
                    key,
                    row["updated_at"],
                    row["expires_at"],
                ),
            )
            return None
        return record

    def list_namespace(
        self,
        *,
        scope: MemoryScope,
        owner_id: str,
        namespace: str,
        now: datetime | None = None,
    ) -> tuple[MemoryRecord, ...]:
        scope = _require_scope(scope)
        owner_id = _required("owner_id", owner_id)
        namespace = _required("namespace", namespace)
        current = _as_utc(now) if now is not None else datetime.now(UTC)
        with self._store.connection() as conn:
            # Compare actual instants, not offset-sensitive ISO strings. Older
            # databases may contain non-UTC timestamps even though put() writes UTC.
            rows = conn.execute(
                "SELECT * FROM memory_records WHERE scope = ? AND owner_id = ? "
                "AND namespace = ? ORDER BY memory_key",
                (scope.value, owner_id, namespace),
            ).fetchall()
            records: list[MemoryRecord] = []
            for row in rows:
                record = _record_from_row(row)
                expiry = record.expires_at
                if expiry is not None and _as_utc(expiry) <= current:
                    conn.execute(
                        "DELETE FROM memory_records WHERE scope = ? AND owner_id = ? "
                        "AND namespace = ? AND memory_key = ? AND updated_at = ? "
                        "AND expires_at = ?",
                        (
                            scope.value,
                            owner_id,
                            namespace,
                            record.key,
                            row["updated_at"],
                            row["expires_at"],
                        ),
                    )
                else:
                    records.append(record)
            # Failure to parse a later row rolls back all scoped deletions.
            return tuple(records)

    def delete(self, *, scope: MemoryScope, owner_id: str, namespace: str, key: str) -> bool:
        scope = _require_scope(scope)
        owner_id = _required("owner_id", owner_id)
        namespace = _required("namespace", namespace)
        key = _required("key", key)
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT * FROM memory_records WHERE scope = ? AND owner_id = ? "
                "AND namespace = ? AND memory_key = ?",
                (scope.value, owner_id, namespace, key),
            ).fetchone()
            if row is None:
                return False
            _record_from_row(row)
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
        scope = _require_scope(scope)
        owner_id = _required("owner_id", owner_id)
        namespace = _required("namespace", namespace)
        key = _required("key", key)
        if type(expected_updated_at) is not datetime:
            raise ValueError("expected_updated_at must be a datetime")
        expected = _as_utc(expected_updated_at)
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT * FROM memory_records WHERE scope = ? AND owner_id = ? "
                "AND namespace = ? AND memory_key = ?",
                (scope.value, owner_id, namespace, key),
            ).fetchone()
            record = _record_from_row(existing) if existing is not None else None
            _require_expected_revision(record, expected)
            cursor = conn.execute(
                "DELETE FROM memory_records WHERE scope = ? AND owner_id = ? "
                "AND namespace = ? AND memory_key = ? AND updated_at = ?",
                (
                    scope.value,
                    owner_id,
                    namespace,
                    key,
                    existing["updated_at"],
                ),
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
        current = _as_utc(now) if now is not None else datetime.now(UTC)
        with self._store.connection() as conn:
            # An explicit global purge must obey the same offset-aware expiry
            # semantics as get() and scoped reads; invalid dates roll back.
            rows = conn.execute(
                "SELECT * FROM memory_records WHERE expires_at IS NOT NULL"
            ).fetchall()
            deleted = 0
            for row in rows:
                record = _record_from_row(row)
                expiry = record.expires_at
                if expiry is not None and _as_utc(expiry) <= current:
                    cursor = conn.execute(
                        "DELETE FROM memory_records WHERE scope = ? AND owner_id = ? "
                        "AND namespace = ? AND memory_key = ? AND updated_at = ? "
                        "AND expires_at = ?",
                        (
                            record.scope.value,
                            record.owner_id,
                            record.namespace,
                            record.key,
                            row["updated_at"],
                            row["expires_at"],
                        ),
                    )
                    deleted += cursor.rowcount
        return deleted


def _require_scope(scope: MemoryScope) -> MemoryScope:
    # Comparing a StrEnum with a raw string is not an admission check.
    if type(scope) is not MemoryScope:
        raise ValueError("scope must be a MemoryScope")
    return scope


def _required(name: str, value: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{name} must be text")
    result = value.strip()
    if not result:
        raise ValueError(f"{name} must not be empty")
    if any(
        unicodedata.category(character) in {"Cc", "Cf", "Zl", "Zp"}
        for character in result
    ):
        raise ValueError(f"{name} must not contain control or invisible characters")
    try:
        result.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{name} must be valid UTF-8") from exc
    return result


def _as_utc(value: datetime) -> datetime:
    if type(value) is not datetime:
        raise ValueError("datetime must be an exact datetime")
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC)


def _next_revision(existing: datetime | None) -> datetime:
    now = datetime.now(UTC)
    if existing is None:
        return now
    previous = _as_utc(existing)
    return max(now, previous + timedelta(microseconds=1))


def _require_expected_revision(
    record: MemoryRecord | None,
    expected: datetime | None | object,
) -> None:
    if expected is _UNCONDITIONAL:
        return
    if expected is None:
        if record is not None:
            raise MemoryConflictError("memory record already exists")
        return
    if record is None:
        raise MemoryConflictError("memory record no longer exists")
    if type(expected) is not datetime:
        raise ValueError("expected_updated_at must be a datetime or None")
    if _as_utc(record.updated_at) != expected:
        raise MemoryConflictError("memory record revision changed")


def _parse_optional(value: Any) -> datetime | None:
    if value is None:
        return None
    return _parse_stored_datetime("expiry", value)


def _parse_stored_datetime(name: str, value: Any) -> datetime:
    if type(value) is not str:
        raise ValueError(f"stored memory {name} must be text")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid stored memory {name}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"stored memory {name} must be timezone-aware")
    return parsed


def _stored_required(name: str, value: Any) -> str:
    if type(value) is not str:
        raise ValueError(f"stored memory {name} must be text")
    normalized = _required(name, value)
    if normalized != value:
        raise ValueError(f"stored memory {name} is not canonical")
    return normalized


def _stored_memory_depth_is_bounded(body: str) -> bool:
    depth = 0
    quoted = False
    escaped = False
    for character in body:
        if quoted:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                quoted = False
        elif character == '"':
            quoted = True
        elif character in "[{":
            depth += 1
            if depth > _MAX_STORED_MEMORY_JSON_DEPTH:
                return False
        elif character in "]}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _bounded_memory_int(number: str) -> int:
    digits = number.removeprefix("-")
    if len(digits) > _MAX_STORED_MEMORY_INTEGER_DECIMAL_CHARS:
        raise ValueError("stored memory JSON integer exceeds the digit limit")
    value = int(number)
    if value.bit_length() > _MAX_STORED_MEMORY_INTEGER_BITS:
        raise ValueError("stored memory JSON integer exceeds the bit limit")
    return value


def _reject_memory_constant(value: str) -> None:
    raise ValueError(f"invalid stored memory JSON constant: {value}")


def _finite_memory_float(number: str) -> float:
    # JSON's finite-looking exponent can overflow the binary float decoder.
    value = float(number)
    if not math.isfinite(value):
        raise ValueError("invalid stored memory JSON number")
    return value


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
    stored_scope = row["scope"]
    if type(stored_scope) is not str:
        raise ValueError("stored memory scope must be text")
    try:
        scope = MemoryScope(stored_scope)
    except ValueError as exc:
        raise ValueError("invalid stored memory scope") from exc
    owner_id = _stored_required("owner_id", row["owner_id"])
    namespace = _stored_required("namespace", row["namespace"])
    key = _stored_required("key", row["memory_key"])
    approval = row["user_approved"]
    if type(approval) is not int or approval not in (0, 1):
        raise ValueError("invalid stored memory approval flag")
    if scope is MemoryScope.USER and approval != 1:
        raise ValueError("user memory lacks durable explicit approval")
    body = row["value_json"]
    if type(body) is not str:
        raise ValueError("stored memory JSON must be text")
    if not _stored_memory_depth_is_bounded(body):
        raise ValueError("stored memory JSON exceeds the depth limit")
    try:
        value = json.loads(
            body,
            parse_constant=_reject_memory_constant,
            parse_float=_finite_memory_float,
            parse_int=_bounded_memory_int,
            object_pairs_hook=_unique_memory_pairs,
        )
    except RecursionError as exc:
        raise ValueError("invalid stored memory JSON") from exc
    _validate_scalar_unicode(value)
    return MemoryRecord(
        scope=scope,
        owner_id=owner_id,
        namespace=namespace,
        key=key,
        value=value,
        user_approved=bool(approval),
        expires_at=_parse_optional(row["expires_at"]),
        created_at=_parse_stored_datetime("created_at", row["created_at"]),
        updated_at=_parse_stored_datetime("updated_at", row["updated_at"]),
    )
