from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from nika_core.memory.contracts import MemoryConflictError, MemoryRecord, MemoryScope
from nika_core.memory.service import MemoryService

SELF_MODEL_NAMESPACE = "nika.self-model.v1"
_TARGET_SCHEMA = "nika-loop-b-self-model-target:v1"
_REVISION_SCHEMA = "nika-loop-b-self-model-revision:v1"
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


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


def _workspace_key(workspace_id: str, facet: str) -> str:
    workspace_digest = hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()
    return f"{workspace_digest}:{facet}"


def self_model_target_ref_sha256(
    *,
    workspace_id: str,
    agent_id: str,
    facet: str,
) -> str:
    canonical_workspace = _require_token(workspace_id, field="workspace_id")
    canonical_agent = _require_token(agent_id, field="agent_id")
    canonical_facet = _require_token(facet, field="facet")
    return _canonical_digest(
        {
            "agent_id": canonical_agent,
            "facet": canonical_facet,
            "schema": _TARGET_SCHEMA,
            "workspace_id": canonical_workspace,
        }
    )


def _as_utc(value: object) -> datetime:
    if type(value) is not datetime:
        raise TypeError("self-model revision must use an exact datetime")
    if value.tzinfo is None:
        raise ValueError("self-model revision datetime must be timezone-aware")
    return value.astimezone(UTC)


def _self_model_revision_sha256(
    record: MemoryRecord,
    *,
    workspace_id: str,
    agent_id: str,
    facet: str,
) -> str:
    if type(record) is not MemoryRecord:
        raise TypeError("self-model record must be an exact MemoryRecord")
    canonical_workspace = _require_token(workspace_id, field="workspace_id")
    canonical_agent = _require_token(agent_id, field="agent_id")
    canonical_facet = _require_token(facet, field="facet")
    expected_key = _workspace_key(canonical_workspace, canonical_facet)
    if (
        record.scope is not MemoryScope.AGENT
        or record.owner_id != canonical_agent
        or record.namespace != SELF_MODEL_NAMESPACE
        or record.key != expected_key
    ):
        raise ValueError("memory record does not belong to the requested self-model target")
    return _canonical_digest(
        {
            "schema": _REVISION_SCHEMA,
            "target_ref_sha256": self_model_target_ref_sha256(
                workspace_id=canonical_workspace,
                agent_id=canonical_agent,
                facet=canonical_facet,
            ),
            "updated_at": _as_utc(record.updated_at).isoformat(),
        }
    )


@dataclass(frozen=True, slots=True)
class SelfModelSnapshot:
    workspace_id: str
    agent_id: str
    facet: str
    value: object
    target_ref_sha256: str
    revision_sha256: str

    def __post_init__(self) -> None:
        _require_token(self.workspace_id, field="workspace_id")
        _require_token(self.agent_id, field="agent_id")
        _require_token(self.facet, field="facet")
        expected_target = self_model_target_ref_sha256(
            workspace_id=self.workspace_id,
            agent_id=self.agent_id,
            facet=self.facet,
        )
        if not hmac.compare_digest(expected_target, self.target_ref_sha256):
            raise ValueError("self-model snapshot target identity is inconsistent")
        _require_sha256(self.revision_sha256, field="revision_sha256")


class SelfModelService:
    """Canonical Loop-B self-model owner backed by the incumbent MemoryService."""

    def __init__(self, memory: MemoryService) -> None:
        if type(memory) is not MemoryService:
            raise TypeError("memory must be the canonical MemoryService")
        self._memory = memory

    def get(
        self,
        *,
        workspace_id: str,
        agent_id: str,
        facet: str,
    ) -> SelfModelSnapshot | None:
        canonical_workspace = _require_token(workspace_id, field="workspace_id")
        canonical_agent = _require_token(agent_id, field="agent_id")
        canonical_facet = _require_token(facet, field="facet")
        record = self._memory.get(
            scope=MemoryScope.AGENT,
            owner_id=canonical_agent,
            namespace=SELF_MODEL_NAMESPACE,
            key=_workspace_key(canonical_workspace, canonical_facet),
        )
        if record is None:
            return None
        return self._snapshot(
            record,
            workspace_id=canonical_workspace,
            agent_id=canonical_agent,
            facet=canonical_facet,
        )

    def compare_and_put(
        self,
        *,
        workspace_id: str,
        agent_id: str,
        facet: str,
        value: object,
        expected_revision_sha256: str | None,
    ) -> SelfModelSnapshot:
        canonical_workspace = _require_token(workspace_id, field="workspace_id")
        canonical_agent = _require_token(agent_id, field="agent_id")
        canonical_facet = _require_token(facet, field="facet")
        expected_updated_at: datetime | None

        if expected_revision_sha256 is None:
            expected_updated_at = None
        else:
            expected_revision = _require_sha256(
                expected_revision_sha256,
                field="expected_revision_sha256",
            )
            current = self._memory.get(
                scope=MemoryScope.AGENT,
                owner_id=canonical_agent,
                namespace=SELF_MODEL_NAMESPACE,
                key=_workspace_key(canonical_workspace, canonical_facet),
            )
            if current is None:
                raise MemoryConflictError("self-model target no longer exists")
            actual_revision = _self_model_revision_sha256(
                current,
                workspace_id=canonical_workspace,
                agent_id=canonical_agent,
                facet=canonical_facet,
            )
            if not hmac.compare_digest(actual_revision, expected_revision):
                raise MemoryConflictError("self-model revision changed")
            expected_updated_at = current.updated_at

        committed = self._memory.compare_and_put(
            scope=MemoryScope.AGENT,
            owner_id=canonical_agent,
            namespace=SELF_MODEL_NAMESPACE,
            key=_workspace_key(canonical_workspace, canonical_facet),
            value=value,
            expected_updated_at=expected_updated_at,
            user_approved=False,
        )
        return self._snapshot(
            committed,
            workspace_id=canonical_workspace,
            agent_id=canonical_agent,
            facet=canonical_facet,
        )

    @staticmethod
    def _snapshot(
        record: MemoryRecord,
        *,
        workspace_id: str,
        agent_id: str,
        facet: str,
    ) -> SelfModelSnapshot:
        return SelfModelSnapshot(
            workspace_id=workspace_id,
            agent_id=agent_id,
            facet=facet,
            value=record.value,
            target_ref_sha256=self_model_target_ref_sha256(
                workspace_id=workspace_id,
                agent_id=agent_id,
                facet=facet,
            ),
            revision_sha256=_self_model_revision_sha256(
                record,
                workspace_id=workspace_id,
                agent_id=agent_id,
                facet=facet,
            ),
        )
