from __future__ import annotations

import hmac
import sqlite3
from dataclasses import dataclass

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
from nika_core.world_model import WorldModelService, world_model_target_ref_sha256

WORLD_MODEL_UPDATE_SCHEMA = "nika.world-model-json/v1"


@dataclass(frozen=True, slots=True)
class LearningWorldModelApplyReceipt:
    intent_sha256: str
    target_ref_sha256: str
    revision_sha256: str
    durable_value_sha256: str
    created: bool


class LearningWorldModelApplier:
    """Apply VERIFIED Loop-B WORLD_MODEL intents through the canonical target owner."""

    def __init__(self, world_model: WorldModelService) -> None:
        if type(world_model) is not WorldModelService:
            raise TypeError("world_model must be the canonical WorldModelService")
        self._world_model = world_model

    @property
    def sqlite_store(self):
        """Return the exact SQLite authority backing this target owner."""
        return self._world_model.sqlite_store

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
        topic: str,
    ) -> LearningWorldModelApplyReceipt:
        return self._apply(
            None,
            intent=intent,
            candidate=candidate,
            verification=verification,
            expected_verification_policy_sha256=expected_verification_policy_sha256,
            expected_requirements=expected_requirements,
            payload=payload,
            workspace_id=workspace_id,
            topic=topic,
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
        topic: str,
    ) -> LearningWorldModelApplyReceipt:
        """Apply one world-model CAS inside a caller-owned SQLite transaction."""
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
            topic=topic,
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
        topic: str,
    ) -> LearningWorldModelApplyReceipt:
        canonical = LearningUpdateIntent.revalidate(
            intent,
            candidate=candidate,
            verification=verification,
            expected_verification_policy_sha256=expected_verification_policy_sha256,
            expected_requirements=expected_requirements,
            payload=payload,
        )
        if canonical.target is not LearningUpdateTarget.WORLD_MODEL:
            raise ValueError("learning world-model adapter accepts only WORLD_MODEL update intents")
        if canonical.update_schema != WORLD_MODEL_UPDATE_SCHEMA:
            raise ValueError("unsupported learning world-model update schema")
        if type(workspace_id) is not str or workspace_id != canonical.workspace_id:
            raise ValueError("world-model workspace does not match cognition scope")

        target_ref = world_model_target_ref_sha256(
            workspace_id=workspace_id,
            topic=topic,
        )
        if not hmac.compare_digest(target_ref, canonical.target_ref_sha256):
            raise ValueError("world-model target does not match bound update intent")

        value = decode_learning_json_payload(payload)
        created = canonical.expected_revision_sha256 is None
        if conn is None:
            snapshot = self._world_model.compare_and_put(
                workspace_id=workspace_id,
                topic=topic,
                value=value,
                expected_revision_sha256=canonical.expected_revision_sha256,
            )
        else:
            snapshot = self._world_model.compare_and_put_with_connection(
                conn,
                workspace_id=workspace_id,
                topic=topic,
                value=value,
                expected_revision_sha256=canonical.expected_revision_sha256,
            )
        return LearningWorldModelApplyReceipt(
            intent_sha256=canonical.intent_sha256,
            target_ref_sha256=snapshot.target_ref_sha256,
            revision_sha256=snapshot.revision_sha256,
            durable_value_sha256=durable_learning_value_sha256(snapshot.value),
            created=created,
        )
