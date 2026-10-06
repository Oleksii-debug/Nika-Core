from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from nika_core.memory.contracts import MemoryConflictError, MemoryRecord, MemoryScope
from nika_core.memory.service import MemoryService

WORLD_MODEL_NAMESPACE = "nika.world-model.v1"
_TARGET_SCHEMA = "nika-loop-b-world-model-target:v1"
_REVISION_SCHEMA = "nika-loop-b-world-model-revision:v1"
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


def _require_token(value: object, *, field: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{field} must be an exact built-in str")
    if _TOKEN_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a bounded machine token")
    return value


def _require_sha256(value: object, *, field: str) -> str:
    if type(value) is not str:
        raise TypeError(f"{field} must be an exact built-in str")
    if _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase 64-character SHA-256 digest")
    return value


def _canonical_digest(payload: dict[str, str]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def world_model_target_ref_sha256(*, workspace_id: str, topic: str) -> str:
    canonical_workspace = _require_token(workspace_id, field="workspace_id")
    canonical_topic = _require_token(topic, field="topic")
    return _canonical_digest(
        {
            "schema": _TARGET_SCHEMA,
            "topic": canonical_topic,
            "workspace_id": canonical_workspace,
        }
    )


def _as_utc(value: object) -> datetime:
    if type(value) is not datetime:
        raise TypeError("world-model revision must use an exact datetime")
    if value.tzinfo is None:
        raise ValueError("world-model revision datetime must be timezone-aware")
    return value.astimezone(UTC)


def _world_model_revision_sha256(
    record: MemoryRecord,
    *,
    workspace_id: str,
    topic: str,
) -> str:
    if type(record) is not MemoryRecord:
        raise TypeError("world-model record must be an exact MemoryRecord")
    canonical_workspace = _require_token(workspace_id, field="workspace_id")
    canonical_topic = _require_token(topic, field="topic")
    if (
        record.scope is not MemoryScope.WORKSPACE
        or record.owner_id != canonical_workspace
        or record.namespace != WORLD_MODEL_NAMESPACE
        or record.key != canonical_topic
    ):
        raise ValueError("memory record does not belong to the requested world-model target")
    return _canonical_digest(
        {
            "schema": _REVISION_SCHEMA,
            "target_ref_sha256": world_model_target_ref_sha256(
                workspace_id=canonical_workspace,
                topic=canonical_topic,
            ),
            "updated_at": _as_utc(record.updated_at).isoformat(),
        }
    )


@dataclass(frozen=True, slots=True)
class WorldModelSnapshot:
    workspace_id: str
    topic: str
    value: object
    target_ref_sha256: str
    revision_sha256: str

    def __post_init__(self) -> None:
        _require_token(self.workspace_id, field="workspace_id")
        _require_token(self.topic, field="topic")
        expected_target = world_model_target_ref_sha256(
            workspace_id=self.workspace_id,
            topic=self.topic,
        )
        if not hmac.compare_digest(expected_target, self.target_ref_sha256):
            raise ValueError("world-model snapshot target identity is inconsistent")
        _require_sha256(self.revision_sha256, field="revision_sha256")


class WorldModelService:
    """Canonical Loop-B workspace world-state owner backed by incumbent MemoryService."""

    def __init__(self, memory: MemoryService) -> None:
        if type(memory) is not MemoryService:
            raise TypeError("memory must be the canonical MemoryService")
        self._memory = memory

    def get(self, *, workspace_id: str, topic: str) -> WorldModelSnapshot | None:
        canonical_workspace = _require_token(workspace_id, field="workspace_id")
        canonical_topic = _require_token(topic, field="topic")
        record = self._memory.get(
            scope=MemoryScope.WORKSPACE,
            owner_id=canonical_workspace,
            namespace=WORLD_MODEL_NAMESPACE,
            key=canonical_topic,
        )
        if record is None:
            return None
        return self._snapshot(
            record,
            workspace_id=canonical_workspace,
            topic=canonical_topic,
        )

    def compare_and_put(
        self,
        *,
        workspace_id: str,
        topic: str,
        value: object,
        expected_revision_sha256: str | None,
    ) -> WorldModelSnapshot:
        canonical_workspace = _require_token(workspace_id, field="workspace_id")
        canonical_topic = _require_token(topic, field="topic")
        expected_updated_at: datetime | None

        if expected_revision_sha256 is None:
            expected_updated_at = None
        else:
            expected_revision = _require_sha256(
                expected_revision_sha256,
                field="expected_revision_sha256",
            )
            current = self._memory.get(
                scope=MemoryScope.WORKSPACE,
                owner_id=canonical_workspace,
                namespace=WORLD_MODEL_NAMESPACE,
                key=canonical_topic,
            )
            if current is None:
                raise MemoryConflictError("world-model target no longer exists")
            actual_revision = _world_model_revision_sha256(
                current,
                workspace_id=canonical_workspace,
                topic=canonical_topic,
            )
            if not hmac.compare_digest(actual_revision, expected_revision):
                raise MemoryConflictError("world-model revision changed")
            expected_updated_at = current.updated_at

        committed = self._memory.compare_and_put(
            scope=MemoryScope.WORKSPACE,
            owner_id=canonical_workspace,
            namespace=WORLD_MODEL_NAMESPACE,
            key=canonical_topic,
            value=value,
            expected_updated_at=expected_updated_at,
            user_approved=False,
        )
        return self._snapshot(
            committed,
            workspace_id=canonical_workspace,
            topic=canonical_topic,
        )

    @staticmethod
    def _snapshot(
        record: MemoryRecord,
        *,
        workspace_id: str,
        topic: str,
    ) -> WorldModelSnapshot:
        return WorldModelSnapshot(
            workspace_id=workspace_id,
            topic=topic,
            value=record.value,
            target_ref_sha256=world_model_target_ref_sha256(
                workspace_id=workspace_id,
                topic=topic,
            ),
            revision_sha256=_world_model_revision_sha256(
                record,
                workspace_id=workspace_id,
                topic=topic,
            ),
        )
