from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.learned_skill import (
    LearnedSkillService,
    learned_skill_target_ref_sha256,
)
from nika_core.learning_application import (
    LearningMemoryTarget,
    LearningMutationApplication,
    LearningSelfModelTarget,
    LearningSkillTarget,
    LearningWorldModelTarget,
)
from nika_core.learning_cognition import (
    CognitionCandidate,
    CognitionCandidateKind,
    CognitionEvidenceRef,
    CognitionVerification,
    CognitionVerificationCheck,
    CognitionVerificationRequirement,
)
from nika_core.learning_memory import (
    MEMORY_UPDATE_SCHEMA,
    LearningMemoryApplier,
    memory_target_ref_sha256,
)
from nika_core.learning_self_model import (
    SELF_MODEL_UPDATE_SCHEMA,
    LearningSelfModelApplier,
)
from nika_core.learning_skill import SKILL_UPDATE_SCHEMA, LearningSkillApplier
from nika_core.learning_update import LearningUpdateIntent, LearningUpdateTarget
from nika_core.learning_world_model import (
    WORLD_MODEL_UPDATE_SCHEMA,
    LearningWorldModelApplier,
)
from nika_core.memory.contracts import MemoryScope
from nika_core.memory.service import MemoryService
from nika_core.self_model import SelfModelService, self_model_target_ref_sha256
from nika_core.world_model import WorldModelService, world_model_target_ref_sha256

A = "a" * 64
B = "b" * 64
C = "c" * 64
POLICY = A
VERIFIER = B
SOURCE_EVIDENCE = C


def _requirements() -> tuple[CognitionVerificationRequirement, ...]:
    return (
        CognitionVerificationRequirement(
            check_id="semantic-truth",
            verifier_sha256=VERIFIER,
        ),
    )


def _candidate(
    *,
    candidate_id: str = "candidate-application-1",
    workspace_id: str = "workspace-1",
    agent_id: str = "agent-1",
) -> CognitionCandidate:
    return CognitionCandidate.create(
        candidate_id=candidate_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
        kind=CognitionCandidateKind.HYPOTHESIS,
        statement="verified semantic application update",
        evidence=(
            CognitionEvidenceRef(
                source_type="comparison",
                source_id="comparison-application-1",
                evidence_sha256=SOURCE_EVIDENCE,
            ),
        ),
    )


def _verification(candidate: CognitionCandidate) -> CognitionVerification:
    return CognitionVerification.create(
        candidate=candidate,
        verification_policy_sha256=POLICY,
        requirements=_requirements(),
        checks=(
            CognitionVerificationCheck(
                check_id="semantic-truth",
                verifier_sha256=VERIFIER,
                evidence_sha256=A,
                passed=True,
            ),
        ),
        expected_verification_policy_sha256=POLICY,
        expected_requirements=_requirements(),
    )


def _store(tmp_path: Path) -> tuple[SQLiteStore, MemoryService]:
    store = SQLiteStore(tmp_path / "learning application" / "nika.db")
    store.initialize()
    return store, MemoryService(store)


def _application(
    tmp_path: Path,
) -> tuple[
    MemoryService,
    WorldModelService,
    SelfModelService,
    LearnedSkillService,
    LearningMutationApplication,
]:
    _store_obj, memory = _store(tmp_path)
    world_model = WorldModelService(memory)
    self_model = SelfModelService(memory)
    skills = LearnedSkillService(memory)
    application = LearningMutationApplication(
        memory=LearningMemoryApplier(memory),
        world_model=LearningWorldModelApplier(world_model),
        self_model=LearningSelfModelApplier(self_model),
        skill=LearningSkillApplier(skills),
    )
    return memory, world_model, self_model, skills, application


def _intent(
    *,
    candidate: CognitionCandidate,
    verification: CognitionVerification,
    intent_id: str,
    target: LearningUpdateTarget,
    target_ref_sha256: str,
    schema: str,
    payload: bytes,
) -> LearningUpdateIntent:
    return LearningUpdateIntent.bind_payload(
        intent_id=intent_id,
        candidate=candidate,
        verification=verification,
        expected_verification_policy_sha256=POLICY,
        expected_requirements=_requirements(),
        target=target,
        target_ref_sha256=target_ref_sha256,
        update_schema=schema,
        payload=payload,
    )


def _apply(
    application: LearningMutationApplication,
    *,
    intent: LearningUpdateIntent,
    candidate: CognitionCandidate,
    verification: CognitionVerification,
    payload: bytes,
    target: object,
):
    return application.apply(
        intent=intent,
        candidate=candidate,
        verification=verification,
        expected_verification_policy_sha256=POLICY,
        expected_requirements=_requirements(),
        payload=payload,
        target=target,
    )


def test_application_dispatches_all_canonical_semantic_targets(
    tmp_path: Path,
) -> None:
    memory, world_model, self_model, skills, application = _application(tmp_path)
    candidate = _candidate()
    verification = _verification(candidate)

    memory_payload = b'{"memory":"bounded"}'
    memory_intent = _intent(
        candidate=candidate,
        verification=verification,
        intent_id="application-memory",
        target=LearningUpdateTarget.MEMORY,
        target_ref_sha256=memory_target_ref_sha256(
            scope=MemoryScope.AGENT,
            owner_id=candidate.agent_id,
            namespace="learning.application",
            key="memory-1",
        ),
        schema=MEMORY_UPDATE_SCHEMA,
        payload=memory_payload,
    )
    memory_receipt = _apply(
        application,
        intent=memory_intent,
        candidate=candidate,
        verification=verification,
        payload=memory_payload,
        target=LearningMemoryTarget(
            scope=MemoryScope.AGENT,
            owner_id=candidate.agent_id,
            namespace="learning.application",
            key="memory-1",
        ),
    )

    world_payload = b'{"world":"bounded"}'
    world_intent = _intent(
        candidate=candidate,
        verification=verification,
        intent_id="application-world",
        target=LearningUpdateTarget.WORLD_MODEL,
        target_ref_sha256=world_model_target_ref_sha256(
            workspace_id=candidate.workspace_id,
            topic="project-state",
        ),
        schema=WORLD_MODEL_UPDATE_SCHEMA,
        payload=world_payload,
    )
    world_receipt = _apply(
        application,
        intent=world_intent,
        candidate=candidate,
        verification=verification,
        payload=world_payload,
        target=LearningWorldModelTarget(
            workspace_id=candidate.workspace_id,
            topic="project-state",
        ),
    )

    self_payload = b'{"self":"bounded"}'
    self_intent = _intent(
        candidate=candidate,
        verification=verification,
        intent_id="application-self",
        target=LearningUpdateTarget.SELF_MODEL,
        target_ref_sha256=self_model_target_ref_sha256(
            workspace_id=candidate.workspace_id,
            agent_id=candidate.agent_id,
            facet="planning",
        ),
        schema=SELF_MODEL_UPDATE_SCHEMA,
        payload=self_payload,
    )
    self_receipt = _apply(
        application,
        intent=self_intent,
        candidate=candidate,
        verification=verification,
        payload=self_payload,
        target=LearningSelfModelTarget(
            workspace_id=candidate.workspace_id,
            agent_id=candidate.agent_id,
            facet="planning",
        ),
    )

    skill_payload = b'{"skill":"bounded"}'
    skill_intent = _intent(
        candidate=candidate,
        verification=verification,
        intent_id="application-skill",
        target=LearningUpdateTarget.SKILL,
        target_ref_sha256=learned_skill_target_ref_sha256(
            workspace_id=candidate.workspace_id,
            agent_id=candidate.agent_id,
            skill_id="document-triage",
        ),
        schema=SKILL_UPDATE_SCHEMA,
        payload=skill_payload,
    )
    skill_receipt = _apply(
        application,
        intent=skill_intent,
        candidate=candidate,
        verification=verification,
        payload=skill_payload,
        target=LearningSkillTarget(
            workspace_id=candidate.workspace_id,
            agent_id=candidate.agent_id,
            skill_id="document-triage",
        ),
    )

    memory_record = memory.get(
        scope=MemoryScope.AGENT,
        owner_id=candidate.agent_id,
        namespace="learning.application",
        key="memory-1",
    )
    world_snapshot = world_model.get(
        workspace_id=candidate.workspace_id,
        topic="project-state",
    )
    self_snapshot = self_model.get(
        workspace_id=candidate.workspace_id,
        agent_id=candidate.agent_id,
        facet="planning",
    )
    skill_snapshot = skills.get(
        workspace_id=candidate.workspace_id,
        agent_id=candidate.agent_id,
        skill_id="document-triage",
    )

    assert memory_record is not None
    assert memory_record.value == {"memory": "bounded"}
    assert world_snapshot is not None
    assert world_snapshot.value == {"world": "bounded"}
    assert self_snapshot is not None
    assert self_snapshot.value == {"self": "bounded"}
    assert skill_snapshot is not None
    assert skill_snapshot.value == {"skill": "bounded"}
    assert {
        memory_receipt.created,
        world_receipt.created,
        self_receipt.created,
        skill_receipt.created,
    } == {True}


def test_wrong_locator_type_fails_before_any_target_effect(tmp_path: Path) -> None:
    memory, world_model, _self_model, _skills, application = _application(tmp_path)
    candidate = _candidate()
    verification = _verification(candidate)
    payload = b'{"memory":"must-not-write"}'
    intent = _intent(
        candidate=candidate,
        verification=verification,
        intent_id="application-wrong-locator",
        target=LearningUpdateTarget.MEMORY,
        target_ref_sha256=memory_target_ref_sha256(
            scope=MemoryScope.AGENT,
            owner_id=candidate.agent_id,
            namespace="learning.application",
            key="memory-wrong",
        ),
        schema=MEMORY_UPDATE_SCHEMA,
        payload=payload,
    )

    with pytest.raises(ValueError, match="requires LearningMemoryTarget"):
        _apply(
            application,
            intent=intent,
            candidate=candidate,
            verification=verification,
            payload=payload,
            target=LearningWorldModelTarget(
                workspace_id=candidate.workspace_id,
                topic="wrong-target",
            ),
        )

    assert memory.get(
        scope=MemoryScope.AGENT,
        owner_id=candidate.agent_id,
        namespace="learning.application",
        key="memory-wrong",
    ) is None
    assert world_model.get(
        workspace_id=candidate.workspace_id,
        topic="wrong-target",
    ) is None


def test_target_owner_revalidates_cognition_before_effect(tmp_path: Path) -> None:
    memory, _world_model, _self_model, _skills, application = _application(tmp_path)
    candidate = _candidate()
    verification = _verification(candidate)
    payload = b'{"memory":"must-not-write"}'
    intent = _intent(
        candidate=candidate,
        verification=verification,
        intent_id="application-cognition-mismatch",
        target=LearningUpdateTarget.MEMORY,
        target_ref_sha256=memory_target_ref_sha256(
            scope=MemoryScope.AGENT,
            owner_id=candidate.agent_id,
            namespace="learning.application",
            key="memory-cognition",
        ),
        schema=MEMORY_UPDATE_SCHEMA,
        payload=payload,
    )
    other_candidate = _candidate(candidate_id="candidate-application-2")

    with pytest.raises(ValueError):
        _apply(
            application,
            intent=intent,
            candidate=other_candidate,
            verification=verification,
            payload=payload,
            target=LearningMemoryTarget(
                scope=MemoryScope.AGENT,
                owner_id=candidate.agent_id,
                namespace="learning.application",
                key="memory-cognition",
            ),
        )

    assert memory.get(
        scope=MemoryScope.AGENT,
        owner_id=candidate.agent_id,
        namespace="learning.application",
        key="memory-cognition",
    ) is None


def test_noncanonical_intent_target_fails_before_dispatch(tmp_path: Path) -> None:
    memory, _world_model, _self_model, _skills, application = _application(tmp_path)
    candidate = _candidate()
    verification = _verification(candidate)
    payload = b'{"memory":"must-not-write"}'
    intent = _intent(
        candidate=candidate,
        verification=verification,
        intent_id="application-forged-target",
        target=LearningUpdateTarget.MEMORY,
        target_ref_sha256=memory_target_ref_sha256(
            scope=MemoryScope.AGENT,
            owner_id=candidate.agent_id,
            namespace="learning.application",
            key="memory-forged",
        ),
        schema=MEMORY_UPDATE_SCHEMA,
        payload=payload,
    )
    object.__setattr__(intent, "target", "memory")

    with pytest.raises(TypeError, match="must be LearningUpdateTarget"):
        _apply(
            application,
            intent=intent,
            candidate=candidate,
            verification=verification,
            payload=payload,
            target=LearningMemoryTarget(
                scope=MemoryScope.AGENT,
                owner_id=candidate.agent_id,
                namespace="learning.application",
                key="memory-forged",
            ),
        )

    assert memory.get(
        scope=MemoryScope.AGENT,
        owner_id=candidate.agent_id,
        namespace="learning.application",
        key="memory-forged",
    ) is None


def test_receipt_does_not_expose_semantic_payload(tmp_path: Path) -> None:
    _memory, _world_model, _self_model, _skills, application = _application(tmp_path)
    candidate = _candidate()
    verification = _verification(candidate)
    secret = "semantic-private-value"
    payload = ('{"note":"' + secret + '"}').encode()
    intent = _intent(
        candidate=candidate,
        verification=verification,
        intent_id="application-private-receipt",
        target=LearningUpdateTarget.SKILL,
        target_ref_sha256=learned_skill_target_ref_sha256(
            workspace_id=candidate.workspace_id,
            agent_id=candidate.agent_id,
            skill_id="privacy-check",
        ),
        schema=SKILL_UPDATE_SCHEMA,
        payload=payload,
    )

    receipt = _apply(
        application,
        intent=intent,
        candidate=candidate,
        verification=verification,
        payload=payload,
        target=LearningSkillTarget(
            workspace_id=candidate.workspace_id,
            agent_id=candidate.agent_id,
            skill_id="privacy-check",
        ),
    )

    assert secret not in repr(receipt)
