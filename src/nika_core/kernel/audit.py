from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

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
        "sig",
        "x_amz_signature",
        "x_amz_credential",
        "x_amz_security_token",
        "x_goog_signature",
        "x_goog_credential",
        "x_goog_security_token",
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
    r"cookie|set[_-]?cookie|secret|sig|signature|credential)\b(\s*[:=]\s*)([^\s,;&]+)"
)
_BEARER_RE: Final = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_PRIVATE_KEY_RE: Final = re.compile(
    r"-----BEGIN [^-\r\n]*PRIVATE KEY-----.*?-----END [^-\r\n]*PRIVATE KEY-----",
    re.IGNORECASE | re.DOTALL,
)
_HTTP_URL_RE: Final = re.compile(r"(?i)https?://[^\s'\"<>]+")
_ENCODED_HTTP_URL_RE: Final = re.compile(
    r"(?i)https?(?:%3a|%253a|%25253a)(?:%2f|%252f|%25252f){2}[^\s'\"<>]+"
)
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
        "sig",
        "signature",
        "x_amz_signature",
        "x_amz_credential",
        "x_amz_security_token",
        "x_goog_signature",
        "x_goog_credential",
        "x_goog_security_token",
        "awsaccesskeyid",
        "googleaccessid",
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
        if payload is not None and not isinstance(payload, dict):
            raise TypeError("audit payload must be a JSON object")
        body = json.dumps(
            {} if payload is None else payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        )
        cursor = conn.execute(
            "INSERT INTO audit_events(event_type, entity_type, entity_id, "
            "payload_json, created_at) "
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
            payload = json.loads(
                row["payload_json"],
                object_pairs_hook=_reject_duplicate_json_keys,
                parse_constant=_reject_nonfinite_json_constant,
                parse_float=_reject_overflow_json_float,
            )
        except (json.JSONDecodeError, TypeError, ValueError, RecursionError) as exc:
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


def _reject_nonfinite_json_constant(_value: str) -> None:
    raise ValueError("noncanonical audit JSON numeric constant")


def _reject_overflow_json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite audit JSON number")
    return number


def _reject_duplicate_json_keys(items: list[tuple[str, object]]) -> dict[str, object]:
    values: dict[str, object] = {}
    for key, value in items:
        if key in values:
            raise ValueError("duplicate audit JSON object key")
        values[key] = value
    return values


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
    sanitized = _ENCODED_HTTP_URL_RE.sub(_redact_encoded_url, sanitized)
    return _HTTP_URL_RE.sub(lambda match: _redact_url(match.group(0)), sanitized)


def _redact_encoded_url(match: re.Match[str]) -> str:
    encoded = match.group(0)
    decoded = encoded
    for _ in range(3):
        decoded_next = unquote(decoded)
        if decoded_next == decoded:
            break
        decoded = decoded_next
    if decoded.casefold().startswith(("http://", "https://")):
        if _redact_url(decoded) != decoded:
            return _REDACTED_URL
    return encoded


def _redact_url(value: str, *, _depth: int = 0) -> str:
    if _depth >= 3:
        return _REDACTED_URL
    try:
        parts = urlsplit(value)
    except ValueError:
        return _REDACTED_URL
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return _REDACTED_URL

    encoded_netloc = parts.netloc
    for _ in range(3):
        decoded_netloc = unquote(encoded_netloc)
        if decoded_netloc == encoded_netloc:
            break
        encoded_netloc = decoded_netloc
        if "@" in encoded_netloc and "@" not in parts.netloc:
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

    safe_query = _redact_url_parameters(parts.query, _depth=_depth)
    safe_fragment = _redact_url_parameters(parts.fragment, _depth=_depth)
    return urlunsplit((parts.scheme, netloc, parts.path, safe_query, safe_fragment))


def _normalized_query_name(name: str) -> str:
    normalized = name.casefold().replace("-", "_")
    for _ in range(3):
        decoded = unquote(normalized).replace("-", "_")
        if decoded == normalized:
            break
        normalized = decoded
    return normalized


def _redact_url_parameters(value: str, *, _depth: int) -> str:
    pairs = parse_qsl(value, keep_blank_values=True)
    safe_pairs: list[tuple[str, str]] = []
    changed = False
    for name, item in pairs:
        if _normalized_query_name(name) in _SECRET_QUERY_NAMES:
            safe_pairs.append((name, _REDACTED))
            changed = True
            continue
        decoded = item
        for _ in range(3):
            if decoded.casefold().startswith(("http://", "https://")):
                break
            decoded_next = unquote(decoded)
            if decoded_next == decoded:
                break
            decoded = decoded_next
        nested_secret = any(
            _normalized_query_name(key) in _SECRET_QUERY_NAMES
            for separator in ("&", ";")
            for key, _ in parse_qsl(
                decoded, keep_blank_values=True, separator=separator
            )
        )
        if nested_secret:
            safe_pairs.append((name, _REDACTED))
            changed = True
            continue
        if decoded.casefold().startswith(("http://", "https://")):
            if _redact_url(decoded, _depth=_depth + 1) != decoded:
                safe_pairs.append((name, _REDACTED_URL))
                changed = True
                continue
        safe_pairs.append((name, item))
    return urlencode(safe_pairs) if changed else value
