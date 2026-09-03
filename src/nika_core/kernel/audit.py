from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from nika_core.data.sqlite import SQLiteStore

_MAX_INSPECTION_LIMIT: Final = 500
_REDACTED: Final = "[REDACTED]"
_REDACTED_URL: Final = "[REDACTED_URL]"
_SENSITIVE_KEYS: Final = frozenset(
    {
        "access_token",
        "api_hash",
        "api_key",
        "authorization",
        "authorization_code",
        "client_secret",
        "cookie",
        "credential_handle",
        "id_token",
        "oauth_code",
        "password",
        "passphrase",
        "private_key",
        "proxy_authorization",
        "refresh_token",
        "session_cookie",
        "session_token",
        "set_cookie",
        "secret",
        "token",
    }
)
_AUTH_HEADER_RE: Final = re.compile(
    r"(?i)\b(authorization|proxy-authorization)(\s*:\s*)[^\r\n]+"
)
_COOKIE_HEADER_RE: Final = re.compile(r"(?i)\b(cookie|set-cookie)(\s*:\s*)[^\r\n]+")
_INLINE_SECRET_RE: Final = re.compile(
    r"(?i)\b(authorization|proxy[_-]?authorization|authorization[_-]?code|oauth[_-]?code|"
    r"api[_-]?key|api[_-]?hash|access[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"session[_-]?token|token|password|passphrase|client[_-]?secret|private[_-]?key|"
    r"cookie|set[_-]?cookie|secret)\b(\s*[:=]\s*)([^\s,;&]+)"
)
_BEARER_RE: Final = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_PRIVATE_KEY_RE: Final = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)
_HTTP_URL_RE: Final = re.compile(r"(?i)https?://[^\s'\"<>]+")
_SECRET_QUERY_NAMES: Final = frozenset(
    {
        "access_token",
        "api_hash",
        "api_key",
        "authorization",
        "authorization_code",
        "client_secret",
        "code",
        "id_token",
        "oauth_code",
        "password",
        "refresh_token",
        "session_token",
        "token",
    }
)


class AuditIntegrityError(RuntimeError):
    """Raised when persisted audit evidence cannot be decoded safely."""


@dataclass(frozen=True, slots=True)
class AuditEvent:
    event_id: int
    event_type: str
    entity_type: str
    entity_id: str
    payload: dict[str, object]
    created_at: str


@dataclass(frozen=True, slots=True)
class AuditInspectionQuery:
    """Bounded forward-only query for user-facing audit inspection."""

    event_type: str | None = None
    entity_type: str | None = None
    entity_id: str | None = None
    after_event_id: int = 0
    limit: int = 100

    def __post_init__(self) -> None:
        for field_name in ("event_type", "entity_type", "entity_id"):
            value = getattr(self, field_name)
            if value is not None and not isinstance(value, str):
                raise TypeError(f"{field_name} must be a string when supplied")
            if value is not None and not value.strip():
                raise ValueError(f"{field_name} must not be blank")
        if type(self.after_event_id) is not int:
            raise TypeError("after_event_id must be an integer")
        if self.after_event_id < 0:
            raise ValueError("after_event_id must be non-negative")
        if type(self.limit) is not int:
            raise TypeError("limit must be an integer")
        if not 1 <= self.limit <= _MAX_INSPECTION_LIMIT:
            raise ValueError(f"limit must be between 1 and {_MAX_INSPECTION_LIMIT}")


@dataclass(frozen=True, slots=True)
class AuditInspectionEvent:
    """Secret-minimized audit event suitable for text/UI presentation."""

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
        return tuple(self._event_from_row(row) for row in rows)

    def inspect(
        self,
        query: AuditInspectionQuery | None = None,
    ) -> tuple[AuditInspectionEvent, ...]:
        """Return a bounded, stable, secret-minimized forward page of audit evidence."""
        request = query or AuditInspectionQuery()
        clauses = ["event_id > ?"]
        parameters: list[object] = [request.after_event_id]

        for column, value in (
            ("event_type", request.event_type),
            ("entity_type", request.entity_type),
            ("entity_id", request.entity_id),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                parameters.append(value)

        parameters.append(request.limit)
        sql = (
            "SELECT event_id, event_type, entity_type, entity_id, payload_json, created_at "
            f"FROM audit_events WHERE {' AND '.join(clauses)} "
            "ORDER BY event_id LIMIT ?"
        )
        with self._store.connection() as conn:
            rows = conn.execute(sql, parameters).fetchall()

        events = tuple(self._event_from_row(row) for row in rows)
        return tuple(
            AuditInspectionEvent(
                event_id=event.event_id,
                event_type=event.event_type,
                entity_type=event.entity_type,
                entity_id=event.entity_id,
                payload=_redact_payload(event.payload),
                created_at=event.created_at,
            )
            for event in events
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> AuditEvent:
        try:
            payload = json.loads(row["payload_json"])
        except (json.JSONDecodeError, TypeError) as exc:
            raise AuditIntegrityError(
                f"audit event {int(row['event_id'])} contains invalid payload JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise AuditIntegrityError(
                f"audit event {int(row['event_id'])} payload must be a JSON object"
            )
        return AuditEvent(
            event_id=int(row["event_id"]),
            event_type=str(row["event_type"]),
            entity_type=str(row["entity_type"]),
            entity_id=str(row["entity_id"]),
            payload=payload,
            created_at=str(row["created_at"]),
        )


def _redact_payload(payload: dict[str, object]) -> dict[str, object]:
    return {str(key): _redact_value(str(key), value) for key, value in payload.items()}


def _redact_value(key: str, value: object) -> object:
    normalized_key = key.casefold().replace("-", "_")
    if _is_sensitive_key(normalized_key):
        return _REDACTED
    if isinstance(value, dict):
        return {
            str(child_key): _redact_value(str(child_key), child)
            for child_key, child in value.items()
        }
    if isinstance(value, list):
        return [_redact_value("", child) for child in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _is_sensitive_key(normalized_key: str) -> bool:
    if normalized_key in _SENSITIVE_KEYS:
        return True
    return normalized_key.endswith(
        (
            "_password",
            "_passphrase",
            "_private_key",
            "_api_key",
            "_api_hash",
            "_token",
            "_client_secret",
            "_authorization",
            "_cookie",
            "_secret",
            "_credential_handle",
        )
    )


def _redact_text(value: str) -> str:
    sanitized = _PRIVATE_KEY_RE.sub(_REDACTED, value)
    sanitized = _AUTH_HEADER_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{_REDACTED}",
        sanitized,
    )
    sanitized = _COOKIE_HEADER_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{_REDACTED}",
        sanitized,
    )
    sanitized = _BEARER_RE.sub("Bearer " + _REDACTED, sanitized)
    sanitized = _INLINE_SECRET_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}{_REDACTED}",
        sanitized,
    )
    return _HTTP_URL_RE.sub(lambda match: _redact_url(match.group(0)), sanitized)


def _redact_url(value: str) -> str:
    try:
        parts = urlsplit(value)
    except ValueError:
        return _REDACTED_URL
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return _REDACTED_URL

    hostname = parts.hostname or ""
    try:
        parsed_port = parts.port
    except ValueError:
        return _REDACTED_URL
    port = f":{parsed_port}" if parsed_port is not None else ""
    if parts.username is not None or parts.password is not None:
        host_for_netloc = f"[{hostname}]" if ":" in hostname else hostname
        netloc = f"{_REDACTED}@{host_for_netloc}{port}"
    else:
        netloc = parts.netloc

    safe_query = _redact_url_parameters(parts.query)
    safe_fragment = _redact_url_parameters(parts.fragment)
    return urlunsplit((parts.scheme, netloc, parts.path, safe_query, safe_fragment))


def _redact_url_parameters(value: str) -> str:
    pairs = parse_qsl(value, keep_blank_values=True)
    sensitive = [
        name.casefold().replace("-", "_") in _SECRET_QUERY_NAMES
        for name, _item in pairs
    ]
    if not any(sensitive):
        return value
    return urlencode(
        [
            (name, _REDACTED if is_sensitive else item)
            for (name, item), is_sensitive in zip(pairs, sensitive, strict=True)
        ]
    )
