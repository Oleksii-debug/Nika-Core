from __future__ import annotations

import hashlib
import hmac
import json
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime

from nika_core.learning_cognition import (
    CognitionCandidate,
    CognitionVerification,
    CognitionVerificationRequirement,
)
from nika_core.learning_payload import (
    decode_learning_json_payload,
    durable_learning_value_sha256,
)
from nika_core.learning_update import LearningUpdateIntent, LearningUpdateTarget
from nika_core.memory.contracts import (
    MemoryConflictError,
    MemoryRecord,
    MemoryScope,
)
from nika_core.memory.service import MemoryService

MEMORY_UPDATE_SCHEMA = "nika.memory-json/v1"
_TARGET_SCHEMA = "nika-loop-b-memory-target:v1"
_REVISION_SCHEMA = "nika-loop-b-memory-revision:v1"
_MAX_TARGET_TEXT_CHARS = 512


@dataclass(frozen=True, slots=True)
class LearningMemoryApplyReceipt:
    intent_sha256: str
    target_ref_sha256: str
    revision_sha256: str
    durable_value_sha256: str
    created: bool


def _require_target_text(value: object, *, field: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{field} must be an exact built-in str")
    if not value or len(value) > _MAX_TARGET_TEXT_CHARS:
        raise ValueError(
            f"{field} must contain 1..{_MAX_TARGET_TEXT_CHARS} characters"
        )
    if value != value.strip():
        raise ValueError(f"{field} must not contain surrounding whitespace")
    if unicodedata.normalize("NFC", value) != value:
        raise ValueError(f"{field} must be NFC-normalized")
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise ValueError(f"{field} must not contain control characters")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ValueError(f"{field} must be valid UTF-8 text") from exc
    return value


def _require_learning_scope(scope: object) -> MemoryScope:
    if type(scope) is not MemoryScope:
        raise TypeError("scope must be an exact MemoryScope")
    if scope not in {MemoryScope.AGENT, MemoryScope.WORKSPACE}:
        raise ValueError("learning memory writes require AGENT or WORKSPACE scope")
    return scope


def memory_target_ref_sha256(
    *,
    scope: MemoryScope,
    owner_id: str,
    namespace: str,
    key: str,
) -> str:
    canonical_scope = _require_learning_scope(scope)
    canonical = {
        "key": _require_target_text(key, field="key"),
        "namespace": _require_target_text(namespace, field="namespace"),
        "owner_id": _require_target_text(owner_id, field="owner_id"),
        "schema": _TARGET_SCHEMA,
        "scope": canonical_scope.value,
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def memory_revision_sha256(record: MemoryRecord) -> str:
    if type(record) is not MemoryRecord:
        raise TypeError("record must be an exact MemoryRecord")
    target_ref = memory_target_ref_sha256(
        scope=record.scope,
        owner_id=record.owner_id,
        namespace=record.namespace,
        key=record.key,
    )
    updated_at = _exact_utc_datetime(record.updated_at)
    canonical = {
        "schema": _REVISION_SCHEMA,
        "target_ref_sha256": target_ref,
        "updated_at": updated_at.isoformat(),
    }
    encoded = json.dumps(
        canonical,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _exact_utc_datetime(value: object) -> datetime:
    if type(value) is not datetime:
        raise TypeError("memory revision must use an exact datetime")
    if value.tzinfo is None:
        raise ValueError("memory revision datetime must be timezone-aware")
    return value.astimezone(UTC)


class LearningMemoryApplier:
    """Apply one VERIFIED Loop-B memory intent through canonical MemoryService CAS."""

    def __init__(self, memory: MemoryService) -> None:
        if type(memory) is not MemoryService:
            raise TypeError("memory must be the canonical MemoryService")
        self._memory = memory

    def apply(
        self,
        *,
        intent: LearningUpdateIntent,
        candidate: CognitionCandidate,
        verification: CognitionVerification,
        expected_verification_policy_sha256: str,
        expected_requirements: tuple[CognitionVerificationRequirement, ...],
        payload: bytes,
        scope: MemoryScope,
        owner_id: str,
        namespace: str,
        key: str,
    ) -> LearningMemoryApplyReceipt:
        canonical = LearningUpdateIntent.revalidate(
            intent,
            candidate=candidate,
            verification=verification,
            expected_verification_policy_sha256=expected_verification_policy_sha256,
            expected_requirements=expected_requirements,
            payload=payload,
        )
        if canonical.target is not LearningUpdateTarget.MEMORY:
            raise ValueError("learning memory adapter accepts only MEMORY update intents")
        if canonical.update_schema != MEMORY_UPDATE_SCHEMA:
            raise ValueError("unsupported learning memory update schema")

        canonical_scope = _require_learning_scope(scope)
        canonical_owner = _require_target_text(owner_id, field="owner_id")
        canonical_namespace = _require_target_text(namespace, field="namespace")
        canonical_key = _require_target_text(key, field="key")
        expected_owner = (
            canonical.agent_id
            if canonical_scope is MemoryScope.AGENT
            else canonical.workspace_id
        )
        if canonical_owner != expected_owner:
            raise ValueError("learning memory target owner does not match cognition scope")

        target_ref = memory_target_ref_sha256(
            scope=canonical_scope,
            owner_id=canonical_owner,
            namespace=canonical_namespace,
            key=canonical_key,
        )
        if not hmac.compare_digest(target_ref, canonical.target_ref_sha256):
            raise ValueError("learning memory target does not match bound update intent")

        value = decode_learning_json_payload(payload)
        expected_revision = canonical.expected_revision_sha256
        created = expected_revision is None
        expected_updated_at: datetime | None
        if expected_revision is None:
            expected_updated_at = None
        else:
            existing = self._memory.get(
                scope=canonical_scope,
                owner_id=canonical_owner,
                namespace=canonical_namespace,
                key=canonical_key,
            )
            if existing is None:
                raise MemoryConflictError("learning memory target no longer exists")
            actual_revision = memory_revision_sha256(existing)
            if not hmac.compare_digest(actual_revision, expected_revision):
                raise MemoryConflictError("learning memory revision changed")
            expected_updated_at = existing.updated_at

        committed = self._memory.compare_and_put(
            scope=canonical_scope,
            owner_id=canonical_owner,
            namespace=canonical_namespace,
            key=canonical_key,
            value=value,
            expected_updated_at=expected_updated_at,
            user_approved=False,
        )
        return LearningMemoryApplyReceipt(
            intent_sha256=canonical.intent_sha256,
            target_ref_sha256=target_ref,
            revision_sha256=memory_revision_sha256(committed),
            durable_value_sha256=durable_learning_value_sha256(committed.value),
            created=created,
        )
