from __future__ import annotations

import hmac
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
from nika_core.self_model import SelfModelService, self_model_target_ref_sha256

SELF_MODEL_UPDATE_SCHEMA = "nika.self-model-json/v1"


@dataclass(frozen=True, slots=True)
class LearningSelfModelApplyReceipt:
    intent_sha256: str
    target_ref_sha256: str
    revision_sha256: str
    durable_value_sha256: str
    created: bool


class LearningSelfModelApplier:
    """Apply VERIFIED Loop-B SELF_MODEL intents through the canonical self-model owner."""

    def __init__(self, self_model: SelfModelService) -> None:
        if type(self_model) is not SelfModelService:
            raise TypeError("self_model must be the canonical SelfModelService")
        self._self_model = self_model

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
        facet: str,
    ) -> LearningSelfModelApplyReceipt:
        canonical = LearningUpdateIntent.revalidate(
            intent,
            candidate=candidate,
            verification=verification,
            expected_verification_policy_sha256=expected_verification_policy_sha256,
            expected_requirements=expected_requirements,
            payload=payload,
        )
        if canonical.target is not LearningUpdateTarget.SELF_MODEL:
            raise ValueError(
                "learning self-model adapter accepts only SELF_MODEL update intents"
            )
        if canonical.update_schema != SELF_MODEL_UPDATE_SCHEMA:
            raise ValueError("unsupported learning self-model update schema")
        if type(workspace_id) is not str or workspace_id != canonical.workspace_id:
            raise ValueError("self-model workspace does not match cognition scope")
        if type(agent_id) is not str or agent_id != canonical.agent_id:
            raise ValueError("self-model agent does not match cognition scope")

        target_ref = self_model_target_ref_sha256(
            workspace_id=workspace_id,
            agent_id=agent_id,
            facet=facet,
        )
        if not hmac.compare_digest(target_ref, canonical.target_ref_sha256):
            raise ValueError("self-model target does not match bound update intent")

        value = decode_learning_json_payload(payload)
        created = canonical.expected_revision_sha256 is None
        snapshot = self._self_model.compare_and_put(
            workspace_id=workspace_id,
            agent_id=agent_id,
            facet=facet,
            value=value,
            expected_revision_sha256=canonical.expected_revision_sha256,
        )
        return LearningSelfModelApplyReceipt(
            intent_sha256=canonical.intent_sha256,
            target_ref_sha256=snapshot.target_ref_sha256,
            revision_sha256=snapshot.revision_sha256,
            durable_value_sha256=durable_learning_value_sha256(snapshot.value),
            created=created,
        )
