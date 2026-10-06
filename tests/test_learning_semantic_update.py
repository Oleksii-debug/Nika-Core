from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.learned_skill import LearnedSkillService, learned_skill_target_ref_sha256
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
from nika_core.learning_semantic_update import (
    LearningSemanticUpdateRouter,
    MemoryUpdateAddress,
    SelfModelUpdateAddress,
    SkillUpdateAddress,
    WorldModelUpdateAddress,
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


def _candidate() -> CognitionCandidate:
    return CognitionCandidate.create(
        candidate_id="candidate-router-1",
        workspace_id="workspace-1",
        agent_id="agent-1",
        kind=CognitionCandidateKind.HYPOTHESIS,
        statement="verified semantic routing evidence",
        evidence=(
            CognitionEvidenceRef(
                source_type="comparison",
                source_id="comparison-router-1",
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


def _router(tmp_path: Path) -> tuple[LearningSemanticUpdateRouter, MemoryService]:
    store = SQLiteStore(tmp_path / "semantic router" / "nika.db")
    store.initialize()
    memory = MemoryService(store)
    return (
        LearningSemanticUpdateRouter(
            memory=LearningMemoryApplier(memory),
            world_model=LearningWorldModelApplier(WorldModelService(memory)),
            self_model=LearningSelfModelApplier(SelfModelService(memory)),
            skill=LearningSkillApplier(LearnedSkillService(memory)),
        ),
        memory,
    )


def _intent(
    *,
    candidate: CognitionCandidate,
    verification: CognitionVerification,
    target: LearningUpdateTarget,
    target_ref_sha256: str,
    update_schema: str,
    payload: bytes,
) -> LearningUpdateIntent:
    return LearningUpdateIntent.bind_payload(
        intent_id=f"router-{target.value}",
        candidate=candidate,
        verification=verification,
        expected_verification_policy_sha256=POLICY,
        expected_requirements=_requirements(),
        target=target,
        target_ref_sha256=target_ref_sha256,
        update_schema=update_schema,
        payload=payload,
    )


def _apply(
    router: LearningSemanticUpdateRouter,
    *,
    intent: LearningUpdateIntent,
    candidate: CognitionCandidate,
    verification: CognitionVerification,
    payload: bytes,
    address: object,
):
    return router.apply(
        intent=intent,
        candidate=candidate,
        verification=verification,
        expected_verification_policy_sha256=POLICY,
        expected_requirements=_requirements(),
        payload=payload,
        address=address,  # type: ignore[arg-type]
    )


def test_router_dispatches_all_four_semantic_targets(tmp_path: Path) -> None:
    router, memory = _router(tmp_path)
    candidate = _candidate()
    verification = _verification(candidate)

    cases = (
        (
            LearningUpdateTarget.MEMORY,
            memory_target_ref_sha256(
                scope=MemoryScope.WORKSPACE,
                owner_id="workspace-1",
                namespace="learned",
                key="fact-1",
            ),
            MEMORY_UPDATE_SCHEMA,
            MemoryUpdateAddress(
                scope=MemoryScope.WORKSPACE,
                owner_id="workspace-1",
                namespace="learned",
                key="fact-1",
            ),
            b'{"kind":"memory"}',
        ),
        (
            LearningUpdateTarget.WORLD_MODEL,
            world_model_target_ref_sha256(
                workspace_id="workspace-1",
                topic="environment",
            ),
            WORLD_MODEL_UPDATE_SCHEMA,
            WorldModelUpdateAddress(
                workspace_id="workspace-1",
                topic="environment",
            ),
            b'{"kind":"world"}',
        ),
        (
            LearningUpdateTarget.SELF_MODEL,
            self_model_target_ref_sha256(
                workspace_id="workspace-1",
                agent_id="agent-1",
                facet="capabilities",
            ),
            SELF_MODEL_UPDATE_SCHEMA,
            SelfModelUpdateAddress(
                workspace_id="workspace-1",
                agent_id="agent-1",
                facet="capabilities",
            ),
            b'{"kind":"self"}',
        ),
        (
            LearningUpdateTarget.SKILL,
            learned_skill_target_ref_sha256(
                workspace_id="workspace-1",
                agent_id="agent-1",
                skill_id="document-triage",
            ),
            SKILL_UPDATE_SCHEMA,
            SkillUpdateAddress(
                workspace_id="workspace-1",
                agent_id="agent-1",
                skill_id="document-triage",
            ),
            b'{"kind":"skill"}',
        ),
    )

    receipts = []
    for target, target_ref, schema, address, payload in cases:
        intent = _intent(
            candidate=candidate,
            verification=verification,
            target=target,
            target_ref_sha256=target_ref,
            update_schema=schema,
            payload=payload,
        )
        receipts.append(
            _apply(
                router,
                intent=intent,
                candidate=candidate,
                verification=verification,
                payload=payload,
                address=address,
            )
        )

    assert len({receipt.target_ref_sha256 for receipt in receipts}) == 4
    assert all(receipt.created is True for receipt in receipts)
    assert memory.get(
        scope=MemoryScope.WORKSPACE,
        owner_id="workspace-1",
        namespace="learned",
        key="fact-1",
    ) is not None


@pytest.mark.parametrize(
    ("target", "target_ref", "schema", "address", "match"),
    (
        (
            LearningUpdateTarget.MEMORY,
            memory_target_ref_sha256(
                scope=MemoryScope.WORKSPACE,
                owner_id="workspace-1",
                namespace="learned",
                key="fact-1",
            ),
            MEMORY_UPDATE_SCHEMA,
            WorldModelUpdateAddress(
                workspace_id="workspace-1",
                topic="environment",
            ),
            "MemoryUpdateAddress",
        ),
        (
            LearningUpdateTarget.WORLD_MODEL,
            world_model_target_ref_sha256(
                workspace_id="workspace-1",
                topic="environment",
            ),
            WORLD_MODEL_UPDATE_SCHEMA,
            SelfModelUpdateAddress(
                workspace_id="workspace-1",
                agent_id="agent-1",
                facet="capabilities",
            ),
            "WorldModelUpdateAddress",
        ),
        (
            LearningUpdateTarget.SELF_MODEL,
            self_model_target_ref_sha256(
                workspace_id="workspace-1",
                agent_id="agent-1",
                facet="capabilities",
            ),
            SELF_MODEL_UPDATE_SCHEMA,
            SkillUpdateAddress(
                workspace_id="workspace-1",
                agent_id="agent-1",
                skill_id="document-triage",
            ),
            "SelfModelUpdateAddress",
        ),
        (
            LearningUpdateTarget.SKILL,
            learned_skill_target_ref_sha256(
                workspace_id="workspace-1",
                agent_id="agent-1",
                skill_id="document-triage",
            ),
            SKILL_UPDATE_SCHEMA,
            MemoryUpdateAddress(
                scope=MemoryScope.WORKSPACE,
                owner_id="workspace-1",
                namespace="learned",
                key="fact-1",
            ),
            "SkillUpdateAddress",
        ),
    ),
)
def test_wrong_address_type_fails_before_any_target_effect(
    tmp_path: Path,
    target: LearningUpdateTarget,
    target_ref: str,
    schema: str,
    address: object,
    match: str,
) -> None:
    router, memory = _router(tmp_path)
    candidate = _candidate()
    verification = _verification(candidate)
    payload = b'{"blocked":true}'
    intent = _intent(
        candidate=candidate,
        verification=verification,
        target=target,
        target_ref_sha256=target_ref,
        update_schema=schema,
        payload=payload,
    )

    with pytest.raises(TypeError, match=match):
        _apply(
            router,
            intent=intent,
            candidate=candidate,
            verification=verification,
            payload=payload,
            address=address,
        )

    assert memory.list_namespace(
        scope=MemoryScope.WORKSPACE,
        owner_id="workspace-1",
        namespace="learned",
    ) == ()
    assert memory.list_namespace(
        scope=MemoryScope.AGENT,
        owner_id="agent-1",
        namespace="nika.self-model.v1",
    ) == ()
    assert memory.list_namespace(
        scope=MemoryScope.AGENT,
        owner_id="agent-1",
        namespace="nika.learned-skill.v1",
    ) == ()


def test_router_does_not_replace_target_specific_revalidation(tmp_path: Path) -> None:
    router, _memory = _router(tmp_path)
    candidate = _candidate()
    verification = _verification(candidate)
    payload = b'{"kind":"world"}'
    good = _intent(
        candidate=candidate,
        verification=verification,
        target=LearningUpdateTarget.WORLD_MODEL,
        target_ref_sha256=world_model_target_ref_sha256(
            workspace_id="workspace-1",
            topic="environment",
        ),
        update_schema=WORLD_MODEL_UPDATE_SCHEMA,
        payload=payload,
    )
    forged = object.__new__(LearningUpdateIntent)
    for field in (
        "intent_id",
        "workspace_id",
        "agent_id",
        "target",
        "target_ref_sha256",
        "candidate_sha256",
        "verification_sha256",
        "update_schema",
        "payload_sha256",
        "payload_bytes",
        "expected_revision_sha256",
    ):
        object.__setattr__(forged, field, getattr(good, field))
    object.__setattr__(forged, "candidate_sha256", B)

    with pytest.raises(ValueError, match="trusted cognition"):
        _apply(
            router,
            intent=forged,
            candidate=candidate,
            verification=verification,
            payload=payload,
            address=WorldModelUpdateAddress(
                workspace_id="workspace-1",
                topic="environment",
            ),
        )


def test_router_rejects_noncanonical_target_before_dispatch(tmp_path: Path) -> None:
    router, _memory = _router(tmp_path)
    candidate = _candidate()
    verification = _verification(candidate)
    payload = b'{"blocked":true}'
    good = _intent(
        candidate=candidate,
        verification=verification,
        target=LearningUpdateTarget.MEMORY,
        target_ref_sha256=memory_target_ref_sha256(
            scope=MemoryScope.WORKSPACE,
            owner_id="workspace-1",
            namespace="learned",
            key="fact-1",
        ),
        update_schema=MEMORY_UPDATE_SCHEMA,
        payload=payload,
    )
    forged = object.__new__(LearningUpdateIntent)
    for field in (
        "intent_id",
        "workspace_id",
        "agent_id",
        "target",
        "target_ref_sha256",
        "candidate_sha256",
        "verification_sha256",
        "update_schema",
        "payload_sha256",
        "payload_bytes",
        "expected_revision_sha256",
    ):
        object.__setattr__(forged, field, getattr(good, field))
    object.__setattr__(forged, "target", "memory")

    with pytest.raises(TypeError, match="canonical LearningUpdateTarget"):
        _apply(
            router,
            intent=forged,
            candidate=candidate,
            verification=verification,
            payload=payload,
            address=MemoryUpdateAddress(
                scope=MemoryScope.WORKSPACE,
                owner_id="workspace-1",
                namespace="learned",
                key="fact-1",
            ),
        )

def test_router_receipt_does_not_expose_semantic_payload(tmp_path: Path) -> None:
    router, _memory = _router(tmp_path)
    candidate = _candidate()
    verification = _verification(candidate)
    secret = "semantic-private-value"
    payload = ('{"note":"' + secret + '"}').encode()
    intent = _intent(
        candidate=candidate,
        verification=verification,
        target=LearningUpdateTarget.SKILL,
        target_ref_sha256=learned_skill_target_ref_sha256(
            workspace_id=candidate.workspace_id,
            agent_id=candidate.agent_id,
            skill_id="privacy-check",
        ),
        update_schema=SKILL_UPDATE_SCHEMA,
        payload=payload,
    )

    receipt = _apply(
        router,
        intent=intent,
        candidate=candidate,
        verification=verification,
        payload=payload,
        address=SkillUpdateAddress(
            workspace_id=candidate.workspace_id,
            agent_id=candidate.agent_id,
            skill_id="privacy-check",
        ),
    )

    assert secret not in repr(receipt)


def test_router_rejects_target_owners_from_different_sqlite_authorities(
    tmp_path: Path,
) -> None:
    primary_store = SQLiteStore(tmp_path / "router-primary.db")
    other_store = SQLiteStore(tmp_path / "router-other.db")
    primary_store.initialize()
    other_store.initialize()
    primary = MemoryService(primary_store)
    other = MemoryService(other_store)

    with pytest.raises(ValueError, match="share one canonical SQLiteStore"):
        LearningSemanticUpdateRouter(
            memory=LearningMemoryApplier(primary),
            world_model=LearningWorldModelApplier(WorldModelService(other)),
            self_model=LearningSelfModelApplier(SelfModelService(primary)),
            skill=LearningSkillApplier(LearnedSkillService(primary)),
        )
