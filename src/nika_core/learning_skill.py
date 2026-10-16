from __future__ import annotations

import hmac
import sqlite3
from dataclasses import dataclass

from nika_core.learned_skill import LearnedSkillService, learned_skill_target_ref_sha256
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

SKILL_UPDATE_SCHEMA = "nika.learned-skill-json/v1"


@dataclass(frozen=True, slots=True)
class LearningSkillApplyReceipt:
    intent_sha256: str
    target_ref_sha256: str
    revision_sha256: str
    durable_value_sha256: str
    created: bool


class LearningSkillApplier:
    """Apply VERIFIED Loop-B SKILL intents through the canonical target owner."""

    def __init__(self, skills: LearnedSkillService) -> None:
        if type(skills) is not LearnedSkillService:
            raise TypeError("skills must be the canonical LearnedSkillService")
        self._skills = skills

    @property
    def sqlite_store(self):
        """Return the exact SQLite authority backing this target owner."""
        return self._skills.sqlite_store

    def apply(
        self,
        *,
        intent: LearningUpdateIntent,
        candidate: CognitionCandidate,
        verification: CognitionVerification,
        expected_verification_policy_sha256: str,
        expected_requirements: tuple[CognitionVerificationRequirement, ...],
        payload: bytes,
        workspace_id: str,
        agent_id: str,
        skill_id: str,
    ) -> LearningSkillApplyReceipt:
        return self._apply(
            None,
            intent=intent,
            candidate=candidate,
            verification=verification,
            expected_verification_policy_sha256=expected_verification_policy_sha256,
            expected_requirements=expected_requirements,
            payload=payload,
            workspace_id=workspace_id,
            agent_id=agent_id,
            skill_id=skill_id,
        )

    def apply_with_connection(
        self,
        conn: sqlite3.Connection,
        *,
        intent: LearningUpdateIntent,
        candidate: CognitionCandidate,
        verification: CognitionVerification,
        expected_verification_policy_sha256: str,
        expected_requirements: tuple[CognitionVerificationRequirement, ...],
        payload: bytes,
        workspace_id: str,
        agent_id: str,
        skill_id: str,
    ) -> LearningSkillApplyReceipt:
        """Apply one learned-skill CAS inside a caller-owned SQLite transaction."""
        if type(conn) is not sqlite3.Connection:
            raise TypeError("conn must be an exact sqlite3.Connection")
        return self._apply(
            conn,
            intent=intent,
            candidate=candidate,
            verification=verification,
            expected_verification_policy_sha256=expected_verification_policy_sha256,
            expected_requirements=expected_requirements,
            payload=payload,
            workspace_id=workspace_id,
            agent_id=agent_id,
            skill_id=skill_id,
        )

    def _apply(
        self,
        conn: sqlite3.Connection | None,
        *,
        intent: LearningUpdateIntent,
        candidate: CognitionCandidate,
        verification: CognitionVerification,
        expected_verification_policy_sha256: str,
        expected_requirements: tuple[CognitionVerificationRequirement, ...],
        payload: bytes,
        workspace_id: str,
        agent_id: str,
        skill_id: str,
    ) -> LearningSkillApplyReceipt:
        canonical = LearningUpdateIntent.revalidate(
            intent,
            candidate=candidate,
            verification=verification,
            expected_verification_policy_sha256=expected_verification_policy_sha256,
            expected_requirements=expected_requirements,
            payload=payload,
        )
        if canonical.target is not LearningUpdateTarget.SKILL:
            raise ValueError("learning skill adapter accepts only SKILL update intents")
        if canonical.update_schema != SKILL_UPDATE_SCHEMA:
            raise ValueError("unsupported learning skill update schema")
        if type(workspace_id) is not str or workspace_id != canonical.workspace_id:
            raise ValueError("learned-skill workspace does not match cognition scope")
        if type(agent_id) is not str or agent_id != canonical.agent_id:
            raise ValueError("learned-skill agent does not match cognition scope")

        target_ref = learned_skill_target_ref_sha256(
            workspace_id=workspace_id,
            agent_id=agent_id,
            skill_id=skill_id,
        )
        if not hmac.compare_digest(target_ref, canonical.target_ref_sha256):
            raise ValueError("learned-skill target does not match bound update intent")

        value = decode_learning_json_payload(payload)
        created = canonical.expected_revision_sha256 is None
        if conn is None:
            snapshot = self._skills.compare_and_put(
                workspace_id=workspace_id,
                agent_id=agent_id,
                skill_id=skill_id,
                value=value,
                expected_revision_sha256=canonical.expected_revision_sha256,
            )
        else:
            snapshot = self._skills.compare_and_put_with_connection(
                conn,
                workspace_id=workspace_id,
                agent_id=agent_id,
                skill_id=skill_id,
                value=value,
                expected_revision_sha256=canonical.expected_revision_sha256,
            )
        return LearningSkillApplyReceipt(
            intent_sha256=canonical.intent_sha256,
            target_ref_sha256=snapshot.target_ref_sha256,
            revision_sha256=snapshot.revision_sha256,
            durable_value_sha256=durable_learning_value_sha256(snapshot.value),
            created=created,
        )
