from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from nika_core.memory.contracts import MemoryConflictError, MemoryRecord, MemoryScope
from nika_core.memory.service import MemoryService

LEARNED_SKILL_NAMESPACE = "nika.learned-skill.v1"
_TARGET_SCHEMA = "nika-loop-b-learned-skill-target:v1"
_REVISION_SCHEMA = "nika-loop-b-learned-skill-revision:v1"
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


def _memory_key(workspace_id: str, skill_id: str) -> str:
    workspace_digest = hashlib.sha256(workspace_id.encode("utf-8")).hexdigest()
    return f"{workspace_digest}:{skill_id}"


def learned_skill_target_ref_sha256(
    *,
    workspace_id: str,
    agent_id: str,
    skill_id: str,
) -> str:
    canonical_workspace = _require_token(workspace_id, field="workspace_id")
    canonical_agent = _require_token(agent_id, field="agent_id")
    canonical_skill = _require_token(skill_id, field="skill_id")
    return _canonical_digest(
        {
            "agent_id": canonical_agent,
            "schema": _TARGET_SCHEMA,
            "skill_id": canonical_skill,
            "workspace_id": canonical_workspace,
        }
    )


def _as_utc(value: object) -> datetime:
    if type(value) is not datetime:
        raise TypeError("learned-skill revision must use an exact datetime")
    if value.tzinfo is None:
        raise ValueError("learned-skill revision datetime must be timezone-aware")
    return value.astimezone(UTC)


def _learned_skill_revision_sha256(
    record: MemoryRecord,
    *,
    workspace_id: str,
    agent_id: str,
    skill_id: str,
) -> str:
    if type(record) is not MemoryRecord:
        raise TypeError("learned-skill record must be an exact MemoryRecord")
    canonical_workspace = _require_token(workspace_id, field="workspace_id")
    canonical_agent = _require_token(agent_id, field="agent_id")
    canonical_skill = _require_token(skill_id, field="skill_id")
    if (
        record.scope is not MemoryScope.AGENT
        or record.owner_id != canonical_agent
        or record.namespace != LEARNED_SKILL_NAMESPACE
        or record.key != _memory_key(canonical_workspace, canonical_skill)
    ):
        raise ValueError("memory record does not belong to the requested learned-skill target")
    return _canonical_digest(
        {
            "schema": _REVISION_SCHEMA,
            "target_ref_sha256": learned_skill_target_ref_sha256(
                workspace_id=canonical_workspace,
                agent_id=canonical_agent,
                skill_id=canonical_skill,
            ),
            "updated_at": _as_utc(record.updated_at).isoformat(),
        }
    )


@dataclass(frozen=True, slots=True)
class LearnedSkillSnapshot:
    workspace_id: str
    agent_id: str
    skill_id: str
    value: object
    target_ref_sha256: str
    revision_sha256: str

    def __post_init__(self) -> None:
        _require_token(self.workspace_id, field="workspace_id")
        _require_token(self.agent_id, field="agent_id")
        _require_token(self.skill_id, field="skill_id")
        expected_target = learned_skill_target_ref_sha256(
            workspace_id=self.workspace_id,
            agent_id=self.agent_id,
            skill_id=self.skill_id,
        )
        if not hmac.compare_digest(expected_target, self.target_ref_sha256):
            raise ValueError("learned-skill snapshot target identity is inconsistent")
        _require_sha256(self.revision_sha256, field="revision_sha256")


class LearnedSkillService:
    """Canonical Loop-B semantic skill owner with no executable capability authority."""

    def __init__(self, memory: MemoryService) -> None:
        if type(memory) is not MemoryService:
            raise TypeError("memory must be the canonical MemoryService")
        self._memory = memory

    def get(
        self,
        *,
        workspace_id: str,
        agent_id: str,
        skill_id: str,
    ) -> LearnedSkillSnapshot | None:
        canonical_workspace = _require_token(workspace_id, field="workspace_id")
        canonical_agent = _require_token(agent_id, field="agent_id")
        canonical_skill = _require_token(skill_id, field="skill_id")
        record = self._memory.get(
            scope=MemoryScope.AGENT,
            owner_id=canonical_agent,
            namespace=LEARNED_SKILL_NAMESPACE,
            key=_memory_key(canonical_workspace, canonical_skill),
        )
        if record is None:
            return None
        return self._snapshot(
            record,
            workspace_id=canonical_workspace,
            agent_id=canonical_agent,
            skill_id=canonical_skill,
        )

    def compare_and_put(
        self,
        *,
        workspace_id: str,
        agent_id: str,
        skill_id: str,
        value: object,
        expected_revision_sha256: str | None,
    ) -> LearnedSkillSnapshot:
        canonical_workspace = _require_token(workspace_id, field="workspace_id")
        canonical_agent = _require_token(agent_id, field="agent_id")
        canonical_skill = _require_token(skill_id, field="skill_id")
        key = _memory_key(canonical_workspace, canonical_skill)
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
                namespace=LEARNED_SKILL_NAMESPACE,
                key=key,
            )
            if current is None:
                raise MemoryConflictError("learned-skill target no longer exists")
            actual_revision = _learned_skill_revision_sha256(
                current,
                workspace_id=canonical_workspace,
                agent_id=canonical_agent,
                skill_id=canonical_skill,
            )
            if not hmac.compare_digest(actual_revision, expected_revision):
                raise MemoryConflictError("learned-skill revision changed")
            expected_updated_at = current.updated_at

        committed = self._memory.compare_and_put(
            scope=MemoryScope.AGENT,
            owner_id=canonical_agent,
            namespace=LEARNED_SKILL_NAMESPACE,
            key=key,
            value=value,
            expected_updated_at=expected_updated_at,
            user_approved=False,
        )
        return self._snapshot(
            committed,
            workspace_id=canonical_workspace,
            agent_id=canonical_agent,
            skill_id=canonical_skill,
        )

    @staticmethod
    def _snapshot(
        record: MemoryRecord,
        *,
        workspace_id: str,
        agent_id: str,
        skill_id: str,
    ) -> LearnedSkillSnapshot:
        return LearnedSkillSnapshot(
            workspace_id=workspace_id,
            agent_id=agent_id,
            skill_id=skill_id,
            value=record.value,
            target_ref_sha256=learned_skill_target_ref_sha256(
                workspace_id=workspace_id,
                agent_id=agent_id,
                skill_id=skill_id,
            ),
            revision_sha256=_learned_skill_revision_sha256(
                record,
                workspace_id=workspace_id,
                agent_id=agent_id,
                skill_id=skill_id,
            ),
        )
