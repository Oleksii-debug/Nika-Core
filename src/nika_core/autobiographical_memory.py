from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from nika_core.data.sqlite import SQLiteStore
from nika_core.memory.contracts import MemoryRecord, MemoryScope
from nika_core.memory.service import MemoryService

_NAMESPACE = "autobiography"
_SCHEMA_VERSION = 1
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_VALUE_KEYS = frozenset(
    {
        "schema_version",
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
        evidence_sha256 = self._audit_event_sha256(safe_event_id)
        key = _memory_key(safe_category, safe_event_id)

        existing = self._memory.get(
            scope=MemoryScope.AGENT,
            owner_id=safe_agent_id,
            namespace=_NAMESPACE,
            key=key,
        )
        if existing is not None:
            entry = self._decode_record(existing)
            if entry.audit_event_sha256 != evidence_sha256:
                raise AutobiographicalMemoryIntegrityError(
                    "autobiographical evidence changed after it was remembered"
                )
            return entry

        record = self._memory.put(
            scope=MemoryScope.AGENT,
            owner_id=safe_agent_id,
            namespace=_NAMESPACE,
            key=key,
            value={
                "schema_version": _SCHEMA_VERSION,
                "category": safe_category.value,
                "audit_event_id": safe_event_id,
                "audit_event_sha256": evidence_sha256,
            },
        )
        return self._decode_record(record)

    def list_entries(self, *, agent_id: str) -> tuple[AutobiographicalEntry, ...]:
        safe_agent_id = _require_token(agent_id, field="agent_id")
        records = self._memory.list_namespace(
            scope=MemoryScope.AGENT,
            owner_id=safe_agent_id,
            namespace=_NAMESPACE,
        )
        entries: list[AutobiographicalEntry] = []
        for record in records:
            entry = self._decode_record(record)
            current_sha256 = self._audit_event_sha256(entry.audit_event_id)
            if current_sha256 != entry.audit_event_sha256:
                raise AutobiographicalMemoryIntegrityError(
                    "autobiographical evidence changed after it was remembered"
                )
            entries.append(entry)
        return tuple(entries)

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
        return self._memory.delete(
            scope=MemoryScope.AGENT,
            owner_id=safe_agent_id,
            namespace=_NAMESPACE,
            key=_memory_key(safe_category, safe_event_id),
        )

    def _audit_event_sha256(self, audit_event_id: int) -> str:
        with self._store.connection() as conn:
            row = conn.execute(
                "SELECT event_type, entity_type, entity_id, payload_json, created_at "
                "FROM audit_events WHERE event_id = ?",
                (audit_event_id,),
            ).fetchone()
        if row is None:
            raise AutobiographicalMemoryIntegrityError(
                "referenced autobiographical audit evidence does not exist"
            )

        event_type = _required_text(row["event_type"], field="event_type")
        entity_type = _required_text(row["entity_type"], field="entity_type")
        entity_id = _required_text(row["entity_id"], field="entity_id")
        payload_json = _canonical_audit_payload(row["payload_json"])
        created_at = _required_timestamp(row["created_at"])
        payload = {
            "event_type": event_type,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "payload_json": payload_json,
            "created_at": created_at,
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _decode_record(self, record: MemoryRecord) -> AutobiographicalEntry:
        if record.scope is not MemoryScope.AGENT or record.namespace != _NAMESPACE:
            raise AutobiographicalMemoryIntegrityError(
                "autobiographical memory record has invalid ownership metadata"
            )
        value = record.value
        if not isinstance(value, dict) or frozenset(value) != _VALUE_KEYS:
            raise AutobiographicalMemoryIntegrityError(
                "autobiographical memory record has invalid schema"
            )
        if type(value.get("schema_version")) is not int or value["schema_version"] != _SCHEMA_VERSION:
            raise AutobiographicalMemoryIntegrityError(
                "autobiographical memory record has unsupported schema"
            )
        try:
            category = AutobiographicalCategory(value.get("category"))
        except (TypeError, ValueError) as exc:
            raise AutobiographicalMemoryIntegrityError(
                "autobiographical memory category is invalid"
            ) from exc
        event_id = value.get("audit_event_id")
        if type(event_id) is not int or event_id <= 0:
            raise AutobiographicalMemoryIntegrityError(
                "autobiographical audit event id is invalid"
            )
        digest = value.get("audit_event_sha256")
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise AutobiographicalMemoryIntegrityError(
                "autobiographical evidence digest is invalid"
            )
        if record.key != _memory_key(category, event_id):
            raise AutobiographicalMemoryIntegrityError(
                "autobiographical memory key does not match its evidence"
            )
        return AutobiographicalEntry(
            category=category,
            audit_event_id=event_id,
            audit_event_sha256=digest,
            remembered_at=record.created_at,
        )


def _require_token(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not _TOKEN_RE.fullmatch(value):
        raise AutobiographicalMemoryError(f"{field} must be a bounded machine token")
    return value


def _require_category(value: object) -> AutobiographicalCategory:
    if not isinstance(value, AutobiographicalCategory):
        raise AutobiographicalMemoryError("category must be an AutobiographicalCategory")
    return value


def _require_event_id(value: object) -> int:
    if type(value) is not int or value <= 0:
        raise AutobiographicalMemoryError("audit_event_id must be a positive integer")
    return value


def _memory_key(category: AutobiographicalCategory, event_id: int) -> str:
    return f"{category.value}:{event_id}"


def _required_text(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AutobiographicalMemoryIntegrityError(f"audit {field} is invalid")
    return value


def _canonical_audit_payload(value: Any) -> str:
    if not isinstance(value, str):
        raise AutobiographicalMemoryIntegrityError("audit payload is invalid")
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise AutobiographicalMemoryIntegrityError("audit payload is invalid JSON") from exc
    if not isinstance(parsed, dict):
        raise AutobiographicalMemoryIntegrityError("audit payload must be a JSON object")
    canonical = json.dumps(
        parsed,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if canonical != value:
        raise AutobiographicalMemoryIntegrityError("audit payload is not canonical")
    return canonical


def _required_timestamp(value: Any) -> str:
    if not isinstance(value, str):
        raise AutobiographicalMemoryIntegrityError("audit timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise AutobiographicalMemoryIntegrityError("audit timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AutobiographicalMemoryIntegrityError("audit timestamp must be timezone-aware")
    return value


__all__ = [
    "AutobiographicalCategory",
    "AutobiographicalEntry",
    "AutobiographicalMemory",
    "AutobiographicalMemoryError",
    "AutobiographicalMemoryIntegrityError",
]
