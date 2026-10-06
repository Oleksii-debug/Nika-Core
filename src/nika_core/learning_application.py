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
from nika_core.learning_skill import (
    LearningSkillApplier,
    LearningSkillApplyReceipt,
)
from nika_core.learning_update import LearningUpdateIntent, LearningUpdateTarget
from nika_core.learning_world_model import (
    LearningWorldModelApplier,
    LearningWorldModelApplyReceipt,
)
from nika_core.memory.contracts import MemoryScope


@dataclass(frozen=True, slots=True)
class LearningMemoryTarget:
    scope: MemoryScope
    owner_id: str
    namespace: str
    key: str


@dataclass(frozen=True, slots=True)
class LearningWorldModelTarget:
    workspace_id: str
    topic: str


@dataclass(frozen=True, slots=True)
class LearningSelfModelTarget:
    workspace_id: str
    agent_id: str
    facet: str


@dataclass(frozen=True, slots=True)
class LearningSkillTarget:
    workspace_id: str
    agent_id: str
    skill_id: str


LearningMutationTarget = (
    LearningMemoryTarget
    | LearningWorldModelTarget
    | LearningSelfModelTarget
    | LearningSkillTarget
)

LearningMutationReceipt = (
    LearningMemoryApplyReceipt
    | LearningWorldModelApplyReceipt
    | LearningSelfModelApplyReceipt
    | LearningSkillApplyReceipt
)


class LearningMutationApplication:
    """Thin application dispatcher over canonical Loop-B semantic target owners."""

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
            raise TypeError(
                "world_model must be the canonical LearningWorldModelApplier"
            )
        if type(self_model) is not LearningSelfModelApplier:
            raise TypeError(
                "self_model must be the canonical LearningSelfModelApplier"
            )
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
        target: LearningMutationTarget,
    ) -> LearningMutationReceipt:
        if type(intent) is not LearningUpdateIntent:
            raise TypeError("intent must be the canonical LearningUpdateIntent")
        try:
            update_target = intent.target
        except AttributeError as exc:
            raise TypeError("learning update intent is missing target") from exc
        if type(update_target) is not LearningUpdateTarget:
            raise TypeError("learning update target must be LearningUpdateTarget")

        common = {
            "intent": intent,
            "candidate": candidate,
            "verification": verification,
            "expected_verification_policy_sha256": (
                expected_verification_policy_sha256
            ),
            "expected_requirements": expected_requirements,
            "payload": payload,
        }
        if update_target is LearningUpdateTarget.MEMORY:
            if type(target) is not LearningMemoryTarget:
                raise ValueError("MEMORY update requires LearningMemoryTarget")
            return self._memory.apply(
                **common,
                scope=target.scope,
                owner_id=target.owner_id,
                namespace=target.namespace,
                key=target.key,
            )
        if update_target is LearningUpdateTarget.WORLD_MODEL:
            if type(target) is not LearningWorldModelTarget:
                raise ValueError(
                    "WORLD_MODEL update requires LearningWorldModelTarget"
                )
            return self._world_model.apply(
                **common,
                workspace_id=target.workspace_id,
                topic=target.topic,
            )
        if update_target is LearningUpdateTarget.SELF_MODEL:
            if type(target) is not LearningSelfModelTarget:
                raise ValueError(
                    "SELF_MODEL update requires LearningSelfModelTarget"
                )
            return self._self_model.apply(
                **common,
                workspace_id=target.workspace_id,
                agent_id=target.agent_id,
                facet=target.facet,
            )
        if update_target is LearningUpdateTarget.SKILL:
            if type(target) is not LearningSkillTarget:
                raise ValueError("SKILL update requires LearningSkillTarget")
            return self._skill.apply(
                **common,
                workspace_id=target.workspace_id,
                agent_id=target.agent_id,
                skill_id=target.skill_id,
            )
        raise ValueError("unsupported learning update target")
