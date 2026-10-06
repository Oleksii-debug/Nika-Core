from __future__ import annotations

from dataclasses import dataclass

from nika_core.learning_cognition import (
    CognitionCandidate,
    CognitionVerification,
    CognitionVerificationRequirement,
)
from nika_core.learning_memory import (
    LearningMemoryApplier,
    LearningMemoryApplyReceipt,
)
from nika_core.learning_self_model import (
    LearningSelfModelApplier,
    LearningSelfModelApplyReceipt,
)
from nika_core.learning_skill import LearningSkillApplier, LearningSkillApplyReceipt
from nika_core.learning_update import LearningUpdateIntent, LearningUpdateTarget
from nika_core.learning_world_model import (
    LearningWorldModelApplier,
    LearningWorldModelApplyReceipt,
)
from nika_core.memory.contracts import MemoryScope


@dataclass(frozen=True, slots=True)
class MemoryUpdateAddress:
    scope: MemoryScope
    owner_id: str
    namespace: str
    key: str


@dataclass(frozen=True, slots=True)
class WorldModelUpdateAddress:
    workspace_id: str
    topic: str


@dataclass(frozen=True, slots=True)
class SelfModelUpdateAddress:
    workspace_id: str
    agent_id: str
    facet: str


@dataclass(frozen=True, slots=True)
class SkillUpdateAddress:
    workspace_id: str
    agent_id: str
    skill_id: str


SemanticUpdateAddress = (
    MemoryUpdateAddress
    | WorldModelUpdateAddress
    | SelfModelUpdateAddress
    | SkillUpdateAddress
)
SemanticUpdateReceipt = (
    LearningMemoryApplyReceipt
    | LearningWorldModelApplyReceipt
    | LearningSelfModelApplyReceipt
    | LearningSkillApplyReceipt
)


class LearningSemanticUpdateRouter:
    """Route one Loop-B intent without owning validation, storage or mutation semantics."""

    def __init__(
        self,
        *,
        memory: LearningMemoryApplier,
        world_model: LearningWorldModelApplier,
        self_model: LearningSelfModelApplier,
        skill: LearningSkillApplier,
    ) -> None:
        if type(memory) is not LearningMemoryApplier:
            raise TypeError("memory must be the canonical LearningMemoryApplier")
        if type(world_model) is not LearningWorldModelApplier:
            raise TypeError("world_model must be the canonical LearningWorldModelApplier")
        if type(self_model) is not LearningSelfModelApplier:
            raise TypeError("self_model must be the canonical LearningSelfModelApplier")
        if type(skill) is not LearningSkillApplier:
            raise TypeError("skill must be the canonical LearningSkillApplier")
        self._memory = memory
        self._world_model = world_model
        self._self_model = self_model
        self._skill = skill

    def apply(
        self,
        *,
        intent: LearningUpdateIntent,
        candidate: CognitionCandidate,
        verification: CognitionVerification,
        expected_verification_policy_sha256: str,
        expected_requirements: tuple[CognitionVerificationRequirement, ...],
        payload: bytes,
        address: SemanticUpdateAddress,
    ) -> SemanticUpdateReceipt:
        if type(intent) is not LearningUpdateIntent:
            raise TypeError("intent must be the canonical LearningUpdateIntent")
        if type(intent.target) is not LearningUpdateTarget:
            raise TypeError("intent target must be the canonical LearningUpdateTarget")

        common = {
            "intent": intent,
            "candidate": candidate,
            "verification": verification,
            "expected_verification_policy_sha256": expected_verification_policy_sha256,
            "expected_requirements": expected_requirements,
            "payload": payload,
        }
        if intent.target is LearningUpdateTarget.MEMORY:
            if type(address) is not MemoryUpdateAddress:
                raise TypeError("MEMORY intent requires MemoryUpdateAddress")
            return self._memory.apply(
                **common,
                scope=address.scope,
                owner_id=address.owner_id,
                namespace=address.namespace,
                key=address.key,
            )
        if intent.target is LearningUpdateTarget.WORLD_MODEL:
            if type(address) is not WorldModelUpdateAddress:
                raise TypeError("WORLD_MODEL intent requires WorldModelUpdateAddress")
            return self._world_model.apply(
                **common,
                workspace_id=address.workspace_id,
                topic=address.topic,
            )
        if intent.target is LearningUpdateTarget.SELF_MODEL:
            if type(address) is not SelfModelUpdateAddress:
                raise TypeError("SELF_MODEL intent requires SelfModelUpdateAddress")
            return self._self_model.apply(
                **common,
                workspace_id=address.workspace_id,
                agent_id=address.agent_id,
                facet=address.facet,
            )
        if intent.target is LearningUpdateTarget.SKILL:
            if type(address) is not SkillUpdateAddress:
                raise TypeError("SKILL intent requires SkillUpdateAddress")
            return self._skill.apply(
                **common,
                workspace_id=address.workspace_id,
                agent_id=address.agent_id,
                skill_id=address.skill_id,
            )
        raise ValueError("unsupported learning update target")
