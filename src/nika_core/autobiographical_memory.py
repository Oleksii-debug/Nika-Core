from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from nika_core.data.sqlite import SQLiteStore
from nika_core.memory.contracts import MemoryScope
from nika_core.memory.service import MemoryService

_NAMESPACE = "autobiography"
_SCHEMA_VERSION = 2
_MAX_SIGNED_64 = (1 << 63) - 1
_MAX_LIST_LIMIT = 100
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_ATTESTATION_FIELD = "autobiographical_category"
_AGENT_ATTESTATION_FIELD = "autobiographical_agent_id"
_VALUE_KEYS = frozenset(
    {
        "schema_version",
        "agent_id",
        "category",
        "audit_event_id",
        "audit_event_sha256",
    }
)


class AutobiographicalMemoryError(ValueError):
    """Base error for autobiographical-memory evidence failures."""


class AutobiographicalMemoryIntegrityError(AutobiographicalMemoryError):
    """Raised when durable autobiographical evidence is missing or tampered."""


class AutobiographicalCategory(StrEnum):
    DECISION = "decision"
    OUTCOME = "outcome"
    MISTAKE = "mistake"
    LESSON = "lesson"
    PROCEDURE = "procedure"
    REPORT_HISTORY = "report_history"


@dataclass(frozen=True, slots=True)
class AutobiographicalEntry:
    category: AutobiographicalCategory
    audit_event_id: int
    audit_event_sha256: str
    remembered_at: datetime


@dataclass(frozen=True, slots=True)
class _AuditEvidence:
    category: AutobiographicalCategory
    agent_id: str
    sha256: str


class AutobiographicalMemory:
    """Privacy-minimized index over immutable canonical audit evidence.

    The index stores no transcript, prompt, response, tool payload, filesystem path,
    provider diagnostic, or free-form summary. Meaning remains anchored to the
    referenced canonical audit event, whose exact durable row is fingerprinted and
    revalidated on every read.
    """

    def __init__(self, store: SQLiteStore, memory: MemoryService) -> None:
        self._store = store
        self._memory = memory

    def remember_audit_event(
        self,
        *,
        agent_id: str,
        category: AutobiographicalCategory,
        audit_event_id: int,
    ) -> AutobiographicalEntry:
        safe_agent_id = _require_token(agent_id, field="agent_id")
        safe_category = _require_category(category)
        safe_event_id = _require_event_id(audit_event_id)
        evidence = self._audit_evidence_many((safe_event_id,))[safe_event_id]
        if evidence.category is not safe_category:
            raise AutobiographicalMemoryIntegrityError(
                "audit evidence does not attest requested autobiographical category"
            )
        if evidence.agent_id != safe_agent_id:
            raise AutobiographicalMemoryIntegrityError(
                "audit evidence does not attest requested autobiographical agent"
            )
        key = _memory_key(safe_category, safe_event_id)

        existing = self._load_record(agent_id=safe_agent_id, key=key)
        if existing is not None:
            if existing.audit_event_sha256 != evidence.sha256:
                raise AutobiographicalMemoryIntegrityError(
                    "autobiographical evidence changed after it was remembered"
                )
            return existing
        if self._last_memory_event(agent_id=safe_agent_id, key=key) == "memory.upserted":
            raise AutobiographicalMemoryIntegrityError(
                "durable autobiographical record is missing after prior persistence"
            )

        self._memory.put(
            scope=MemoryScope.AGENT,
            owner_id=safe_agent_id,
            namespace=_NAMESPACE,
            key=key,
            value={
                "schema_version": _SCHEMA_VERSION,
                "agent_id": safe_agent_id,
                "category": safe_category.value,
                "audit_event_id": safe_event_id,
                "audit_event_sha256": evidence.sha256,
            },
        )
        entry = self._load_record(agent_id=safe_agent_id, key=key)
        if entry is None:
            raise AutobiographicalMemoryIntegrityError(
                "durable autobiographical record disappeared after persistence"
            )
        current = self._audit_evidence_many((safe_event_id,))[safe_event_id]
        if (
            current.category is not safe_category
            or current.agent_id != safe_agent_id
            or current.sha256 != evidence.sha256
        ):
            self._memory.delete(
                scope=MemoryScope.AGENT,
                owner_id=safe_agent_id,
                namespace=_NAMESPACE,
                key=key,
            )
            raise AutobiographicalMemoryIntegrityError(
                "autobiographical evidence changed while it was remembered"
            )
        return entry

    def list_entries(
        self, *, agent_id: str, limit: int
    ) -> tuple[AutobiographicalEntry, ...]:
        safe_agent_id = _require_token(agent_id, field="agent_id")
        safe_limit = _require_limit(limit)
        rows = self._list_rows(agent_id=safe_agent_id, limit=safe_limit)
        entries = tuple(
            self._decode_row(row, expected_agent_id=safe_agent_id) for row in rows
        )
        evidence_by_id = self._audit_evidence_many(
            tuple(entry.audit_event_id for entry in entries)
        )
        for entry in entries:
            evidence = evidence_by_id[entry.audit_event_id]
            if evidence.category is not entry.category:
                raise AutobiographicalMemoryIntegrityError(
                    "audit evidence no longer attests autobiographical category"
                )
            if evidence.agent_id != safe_agent_id:
                raise AutobiographicalMemoryIntegrityError(
                    "audit evidence no longer attests autobiographical agent"
                )
            if evidence.sha256 != entry.audit_event_sha256:
                raise AutobiographicalMemoryIntegrityError(
                    "autobiographical evidence changed after it was remembered"
                )
        return entries

    def forget_audit_event(
        self,
        *,
        agent_id: str,
        category: AutobiographicalCategory,
        audit_event_id: int,
    ) -> bool:
        safe_agent_id = _require_token(agent_id, field="agent_id")
        safe_category = _require_category(category)
        safe_event_id = _require_event_id(audit_event_id)
        key = _memory_key(safe_category, safe_event_id)
        existing = self._load_record(agent_id=safe_agent_id, key=key)
        if existing is None:
            return False
        return self._memory.delete(
            scope=MemoryScope.AGENT,
            owner_id=safe_agent_id,
            namespace=_NAMESPACE,
            key=key,
        )

    def _load_record(
        self, *, agent_id: str, key: str
    ) -> AutobiographicalEntry | None:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT scope, owner_id, namespace, memory_key, value_json, "
                "user_approved, expires_at, created_at, updated_at "
                "FROM memory_records WHERE scope = ? AND owner_id = ? "
                "AND namespace = ? AND memory_key = ?",
                (MemoryScope.AGENT.value, agent_id, _NAMESPACE, key),
            ).fetchone()
        if row is None:
            return None
        return self._decode_row(row, expected_agent_id=agent_id)

    def _list_rows(self, *, agent_id: str, limit: int) -> tuple[Any, ...]:
        with self._store.connection() as conn:
            rows = conn.execute(
                "SELECT scope, owner_id, namespace, memory_key, value_json, "
                "user_approved, expires_at, created_at, updated_at "
                "FROM memory_records WHERE scope = ? AND owner_id = ? "
                "AND namespace = ? ORDER BY created_at DESC, memory_key DESC LIMIT ?",
                (MemoryScope.AGENT.value, agent_id, _NAMESPACE, limit),
            ).fetchall()
        return tuple(rows)

    def _last_memory_event(self, *, agent_id: str, key: str) -> str | None:
        entity_id = f"{MemoryScope.AGENT.value}:{agent_id}:{_NAMESPACE}:{key}"
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT event_type FROM audit_events WHERE entity_type = ? "
                "AND entity_id = ? ORDER BY event_id DESC LIMIT 1",
                ("memory", entity_id),
            ).fetchone()
        if row is None:
            return None
        event_type = _required_text(row["event_type"], field="event_type")
        if event_type not in {"memory.upserted", "memory.deleted"}:
            raise AutobiographicalMemoryIntegrityError(
                "autobiographical memory audit history is invalid"
            )
        return event_type

    def _audit_evidence_many(
        self, audit_event_ids: tuple[int, ...]
    ) -> dict[int, _AuditEvidence]:
        if not audit_event_ids:
            return {}
        unique_ids = tuple(dict.fromkeys(audit_event_ids))
        placeholders = ",".join("?" for _ in unique_ids)
        with self._store.connection() as conn:
            rows = conn.execute(
                "SELECT event_id, event_type, entity_type, entity_id, payload_json, "
                f"created_at FROM audit_events WHERE event_id IN ({placeholders})",
                unique_ids,
            ).fetchall()
        evidence: dict[int, _AuditEvidence] = {}
        for row in rows:
            event_id = _require_event_id(row["event_id"])
            if event_id in evidence:
                raise AutobiographicalMemoryIntegrityError(
                    "duplicate autobiographical audit evidence"
                )
            evidence[event_id] = _audit_evidence_from_row(row)
        missing = set(unique_ids).difference(evidence)
        if missing:
            raise AutobiographicalMemoryIntegrityError(
                "referenced autobiographical audit evidence does not exist"
            )
        return evidence

    def _decode_row(self, row: Any, *, expected_agent_id: str) -> AutobiographicalEntry:
        if row["scope"] != MemoryScope.AGENT.value or row["namespace"] != _NAMESPACE:
            raise AutobiographicalMemoryIntegrityError(
                "autobiographical memory record has invalid ownership metadata"
            )
        owner_id = _require_token(row["owner_id"], field="durable owner_id")
        if owner_id != expected_agent_id:
            raise AutobiographicalMemoryIntegrityError(
                "autobiographical memory owner does not match requested agent"
            )
        if type(row["user_approved"]) is not int or row["user_approved"] != 0:
            raise AutobiographicalMemoryIntegrityError(
                "autobiographical memory approval metadata is invalid"
            )
        if row["expires_at"] is not None:
            raise AutobiographicalMemoryIntegrityError(
                "autobiographical memory must not have an expiry"
            )
        value = _canonical_memory_value(row["value_json"])
        schema_version = value.get("schema_version")
        if type(schema_version) is not int or schema_version != _SCHEMA_VERSION:
            raise AutobiographicalMemoryIntegrityError(
                "autobiographical memory record has unsupported schema"
            )
        durable_agent_id = _require_token(value.get("agent_id"), field="durable agent_id")
        if durable_agent_id != expected_agent_id or durable_agent_id != owner_id:
            raise AutobiographicalMemoryIntegrityError(
                "autobiographical memory owner binding is invalid"
            )
        category = _decode_category(value.get("category"))
        event_id = value.get("audit_event_id")
        if type(event_id) is not int or not 1 <= event_id <= _MAX_SIGNED_64:
            raise AutobiographicalMemoryIntegrityError(
                "autobiographical audit event id is invalid"
            )
        digest = value.get("audit_event_sha256")
        if type(digest) is not str or not _SHA256_RE.fullmatch(digest):
            raise AutobiographicalMemoryIntegrityError(
                "autobiographical evidence digest is invalid"
            )
        if row["memory_key"] != _memory_key(category, event_id):
            raise AutobiographicalMemoryIntegrityError(
                "autobiographical memory key does not match its evidence"
            )
        remembered_at = _parse_memory_timestamp(row["created_at"], field="created_at")
        updated_at = _parse_memory_timestamp(row["updated_at"], field="updated_at")
        if updated_at < remembered_at:
            raise AutobiographicalMemoryIntegrityError(
                "autobiographical memory timestamps are inconsistent"
            )
        return AutobiographicalEntry(
            category=category,
            audit_event_id=event_id,
            audit_event_sha256=digest,
            remembered_at=remembered_at,
        )


def _audit_evidence_from_row(row: Any) -> _AuditEvidence:
    event_type = _required_text(row["event_type"], field="event_type")
    entity_type = _required_text(row["entity_type"], field="entity_type")
    entity_id = _required_text(row["entity_id"], field="entity_id")
    payload_json, payload = _canonical_audit_payload(row["payload_json"])
    created_at = _required_timestamp(row["created_at"])
    category = _decode_attested_category(payload.get(_ATTESTATION_FIELD))
    agent_id = _decode_attested_agent(payload.get(_AGENT_ATTESTATION_FIELD))
    encoded = json.dumps(
        {
            "event_type": event_type,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "payload_json": payload_json,
            "created_at": created_at,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return _AuditEvidence(
        category=category,
        agent_id=agent_id,
        sha256=hashlib.sha256(encoded).hexdigest(),
    )


def _require_token(value: object, *, field: str) -> str:
    if type(value) is not str or not _TOKEN_RE.fullmatch(value):
        raise AutobiographicalMemoryError(f"{field} must be a bounded machine token")
    return value


def _require_category(value: object) -> AutobiographicalCategory:
    if type(value) is not AutobiographicalCategory:
        raise AutobiographicalMemoryError("category must be an AutobiographicalCategory")
    return value


def _require_event_id(value: object) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_SIGNED_64:
        raise AutobiographicalMemoryError(
            "audit_event_id must be a positive signed-64 integer"
        )
    return value


def _require_limit(value: object) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_LIST_LIMIT:
        raise AutobiographicalMemoryError(
            f"limit must be an integer from 1 through {_MAX_LIST_LIMIT}"
        )
    return value


def _memory_key(category: AutobiographicalCategory, event_id: int) -> str:
    return f"{category.value}:{event_id}"


def _required_text(value: Any, *, field: str) -> str:
    if type(value) is not str or not value.strip():
        raise AutobiographicalMemoryIntegrityError(f"audit {field} is invalid")
    return value


def _canonical_audit_payload(value: Any) -> tuple[str, dict[str, Any]]:
    if type(value) is not str:
        raise AutobiographicalMemoryIntegrityError("audit payload is invalid")
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise AutobiographicalMemoryIntegrityError("audit payload is invalid JSON") from exc
    if type(parsed) is not dict:
        raise AutobiographicalMemoryIntegrityError(
            "audit payload must be a JSON object"
        )
    try:
        canonical = json.dumps(
            parsed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise AutobiographicalMemoryIntegrityError(
            "audit payload contains unsupported JSON values"
        ) from exc
    if canonical != value:
        raise AutobiographicalMemoryIntegrityError("audit payload is not canonical")
    return canonical, parsed


def _canonical_memory_value(value: Any) -> dict[str, Any]:
    if type(value) is not str:
        raise AutobiographicalMemoryIntegrityError(
            "autobiographical memory record has invalid schema"
        )
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise AutobiographicalMemoryIntegrityError(
            "autobiographical memory record has invalid schema"
        ) from exc
    if type(parsed) is not dict or frozenset(parsed) != _VALUE_KEYS:
        raise AutobiographicalMemoryIntegrityError(
            "autobiographical memory record has invalid schema"
        )
    try:
        canonical = json.dumps(
            parsed,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise AutobiographicalMemoryIntegrityError(
            "autobiographical memory record has invalid schema"
        ) from exc
    if canonical != value:
        raise AutobiographicalMemoryIntegrityError(
            "autobiographical memory record is not canonical"
        )
    return parsed


def _decode_category(value: object) -> AutobiographicalCategory:
    if type(value) is not str:
        raise AutobiographicalMemoryIntegrityError(
            "autobiographical memory category is invalid"
        )
    try:
        return AutobiographicalCategory(value)
    except ValueError as exc:
        raise AutobiographicalMemoryIntegrityError(
            "autobiographical memory category is invalid"
        ) from exc


def _decode_attested_agent(value: object) -> str:
    try:
        return _require_token(value, field="audit autobiographical_agent_id")
    except AutobiographicalMemoryError as exc:
        raise AutobiographicalMemoryIntegrityError(
            "audit evidence lacks valid autobiographical agent attestation"
        ) from exc


def _decode_attested_category(value: object) -> AutobiographicalCategory:
    if type(value) is not str:
        raise AutobiographicalMemoryIntegrityError(
            "audit evidence lacks autobiographical category attestation"
        )
    try:
        return AutobiographicalCategory(value)
    except ValueError as exc:
        raise AutobiographicalMemoryIntegrityError(
            "audit autobiographical category attestation is invalid"
        ) from exc


def _required_timestamp(value: Any) -> str:
    if type(value) is not str:
        raise AutobiographicalMemoryIntegrityError("audit timestamp is invalid")
    parsed = _parse_aware_timestamp(value, field="audit timestamp")
    if parsed.isoformat() != value:
        raise AutobiographicalMemoryIntegrityError("audit timestamp is not canonical")
    return value


def _parse_memory_timestamp(value: Any, *, field: str) -> datetime:
    if type(value) is not str:
        raise AutobiographicalMemoryIntegrityError(f"memory {field} is invalid")
    parsed = _parse_aware_timestamp(value, field=f"memory {field}")
    if parsed.isoformat() != value:
        raise AutobiographicalMemoryIntegrityError(
            f"memory {field} is not canonical"
        )
    return parsed


def _parse_aware_timestamp(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise AutobiographicalMemoryIntegrityError(f"{field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AutobiographicalMemoryIntegrityError(f"{field} must be timezone-aware")
    return parsed


__all__ = [
    "AutobiographicalCategory",
    "AutobiographicalEntry",
    "AutobiographicalMemory",
    "AutobiographicalMemoryError",
    "AutobiographicalMemoryIntegrityError",
]
