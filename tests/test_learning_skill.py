from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.learned_skill import (
    LEARNED_SKILL_NAMESPACE,
    LearnedSkillService,
    learned_skill_target_ref_sha256,
)
from nika_core.learning_cognition import (
    CognitionCandidate,
    CognitionCandidateKind,
    CognitionEvidenceRef,
    CognitionVerification,
    CognitionVerificationCheck,
    CognitionVerificationRequirement,
)
from nika_core.learning_skill import SKILL_UPDATE_SCHEMA, LearningSkillApplier
from nika_core.learning_update import LearningUpdateIntent, LearningUpdateTarget
from nika_core.memory.contracts import MemoryConflictError, MemoryScope
from nika_core.memory.service import MemoryService

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
    candidate_id: str = "candidate-skill-1",
    workspace_id: str = "workspace-1",
    agent_id: str = "agent-1",
) -> CognitionCandidate:
    return CognitionCandidate.create(
        candidate_id=candidate_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
        kind=CognitionCandidateKind.HYPOTHESIS,
        statement="verified bounded procedural learning",
        evidence=(
            CognitionEvidenceRef(
                source_type="comparison",
                source_id="comparison-skill-1",
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
    store = SQLiteStore(tmp_path / "learned skill" / "nika.db")
    store.initialize()
    return store, MemoryService(store)


def _intent(
    *,
    candidate: CognitionCandidate,
    verification: CognitionVerification,
    payload: bytes,
    skill_id: str = "document-triage",
    target: LearningUpdateTarget = LearningUpdateTarget.SKILL,
    schema: str = SKILL_UPDATE_SCHEMA,
    target_ref_sha256: str | None = None,
    expected_revision_sha256: str | None = None,
) -> LearningUpdateIntent:
    return LearningUpdateIntent.bind_payload(
        intent_id=f"skill-update-{candidate.candidate_id}",
        candidate=candidate,
        verification=verification,
        expected_verification_policy_sha256=POLICY,
        expected_requirements=_requirements(),
        target=target,
        target_ref_sha256=target_ref_sha256
        or learned_skill_target_ref_sha256(
            workspace_id=candidate.workspace_id,
            agent_id=candidate.agent_id,
            skill_id=skill_id,
        ),
        update_schema=schema,
        payload=payload,
        expected_revision_sha256=expected_revision_sha256,
    )


def _apply(
    applier: LearningSkillApplier,
    *,
    intent: LearningUpdateIntent,
    candidate: CognitionCandidate,
    verification: CognitionVerification,
    payload: bytes,
    workspace_id: str = "workspace-1",
    agent_id: str = "agent-1",
    skill_id: str = "document-triage",
):
    return applier.apply(
        intent=intent,
        candidate=candidate,
        verification=verification,
        expected_verification_policy_sha256=POLICY,
        expected_requirements=_requirements(),
        payload=payload,
        workspace_id=workspace_id,
        agent_id=agent_id,
        skill_id=skill_id,
    )


def test_verified_skill_create_is_semantic_only_and_minimized(tmp_path: Path) -> None:
    _store_obj, memory = _store(tmp_path)
    skills = LearnedSkillService(memory)
    candidate = _candidate()
    verification = _verification(candidate)
    secret = "sk-learned-skill-secret"
    payload = (
        '{"steps":["inspect","classify"],"api_key":"' + secret + '"}'
    ).encode()
    intent = _intent(candidate=candidate, verification=verification, payload=payload)

    receipt = _apply(
        LearningSkillApplier(skills),
        intent=intent,
        candidate=candidate,
        verification=verification,
        payload=payload,
    )
    snapshot = skills.get(
        workspace_id="workspace-1",
        agent_id="agent-1",
        skill_id="document-triage",
    )
    records = memory.list_namespace(
        scope=MemoryScope.AGENT,
        owner_id="agent-1",
        namespace=LEARNED_SKILL_NAMESPACE,
    )

    assert snapshot is not None
    assert snapshot.value == {
        "api_key": "[REDACTED]",
        "steps": ["inspect", "classify"],
    }
    assert len(records) == 1
    assert receipt.created is True
    assert receipt.target_ref_sha256 == intent.target_ref_sha256
    assert receipt.revision_sha256 == snapshot.revision_sha256
    assert secret not in repr(receipt)
    assert set(snapshot.__dataclass_fields__) == {
        "workspace_id",
        "agent_id",
        "skill_id",
        "value",
        "target_ref_sha256",
        "revision_sha256",
    }


def test_skill_state_is_agent_and_workspace_scoped(tmp_path: Path) -> None:
    _store_obj, memory = _store(tmp_path)
    skills = LearnedSkillService(memory)
    candidate = _candidate()
    verification = _verification(candidate)
    payload = b'{"procedure":"bounded"}'
    intent = _intent(candidate=candidate, verification=verification, payload=payload)

    _apply(
        LearningSkillApplier(skills),
        intent=intent,
        candidate=candidate,
        verification=verification,
        payload=payload,
    )

    assert skills.get(
        workspace_id="workspace-1",
        agent_id="agent-1",
        skill_id="document-triage",
    ) is not None
    assert skills.get(
        workspace_id="workspace-2",
        agent_id="agent-1",
        skill_id="document-triage",
    ) is None
    assert skills.get(
        workspace_id="workspace-1",
        agent_id="agent-2",
        skill_id="document-triage",
    ) is None


def test_exact_revision_updates_and_restart_recovers_skill(tmp_path: Path) -> None:
    store, memory = _store(tmp_path)
    skills = LearnedSkillService(memory)
    first_candidate = _candidate()
    first_verification = _verification(first_candidate)
    first_payload = b'{"generation":1}'
    first_intent = _intent(
        candidate=first_candidate,
        verification=first_verification,
        payload=first_payload,
    )
    first = _apply(
        LearningSkillApplier(skills),
        intent=first_intent,
        candidate=first_candidate,
        verification=first_verification,
        payload=first_payload,
    )

    second_candidate = _candidate(candidate_id="candidate-skill-2")
    second_verification = _verification(second_candidate)
    second_payload = b'{"generation":2}'
    second_intent = _intent(
        candidate=second_candidate,
        verification=second_verification,
        payload=second_payload,
        expected_revision_sha256=first.revision_sha256,
    )
    second = _apply(
        LearningSkillApplier(skills),
        intent=second_intent,
        candidate=second_candidate,
        verification=second_verification,
        payload=second_payload,
    )

    restarted = LearnedSkillService(MemoryService(store))
    snapshot = restarted.get(
        workspace_id="workspace-1",
        agent_id="agent-1",
        skill_id="document-triage",
    )
    assert second.created is False
    assert snapshot is not None
    assert snapshot.value == {"generation": 2}
    assert snapshot.revision_sha256 == second.revision_sha256


def test_missing_revision_is_create_only(tmp_path: Path) -> None:
    _store_obj, memory = _store(tmp_path)
    skills = LearnedSkillService(memory)
    skills.compare_and_put(
        workspace_id="workspace-1",
        agent_id="agent-1",
        skill_id="document-triage",
        value={"generation": 1},
        expected_revision_sha256=None,
    )
    candidate = _candidate()
    verification = _verification(candidate)
    payload = b'{"generation":2}'
    intent = _intent(candidate=candidate, verification=verification, payload=payload)

    with pytest.raises(MemoryConflictError):
        _apply(
            LearningSkillApplier(skills),
            intent=intent,
            candidate=candidate,
            verification=verification,
            payload=payload,
        )

    snapshot = skills.get(
        workspace_id="workspace-1",
        agent_id="agent-1",
        skill_id="document-triage",
    )
    assert snapshot is not None and snapshot.value == {"generation": 1}


def test_stale_revision_preserves_newer_skill_state(tmp_path: Path) -> None:
    _store_obj, memory = _store(tmp_path)
    skills = LearnedSkillService(memory)
    original = skills.compare_and_put(
        workspace_id="workspace-1",
        agent_id="agent-1",
        skill_id="document-triage",
        value={"generation": 1},
        expected_revision_sha256=None,
    )
    winner = skills.compare_and_put(
        workspace_id="workspace-1",
        agent_id="agent-1",
        skill_id="document-triage",
        value={"generation": 2},
        expected_revision_sha256=original.revision_sha256,
    )
    candidate = _candidate()
    verification = _verification(candidate)
    payload = b'{"generation":3}'
    intent = _intent(
        candidate=candidate,
        verification=verification,
        payload=payload,
        expected_revision_sha256=original.revision_sha256,
    )

    with pytest.raises(MemoryConflictError, match="revision changed"):
        _apply(
            LearningSkillApplier(skills),
            intent=intent,
            candidate=candidate,
            verification=verification,
            payload=payload,
        )

    assert skills.get(
        workspace_id="workspace-1",
        agent_id="agent-1",
        skill_id="document-triage",
    ) == winner


@pytest.mark.parametrize(
    ("workspace_id", "agent_id", "skill_id", "match"),
    (
        ("workspace-2", "agent-1", "document-triage", "workspace"),
        ("workspace-1", "agent-2", "document-triage", "agent"),
        ("workspace-1", "agent-1", "other-skill", "target does not match"),
    ),
)
def test_bound_skill_target_cannot_be_rebound(
    tmp_path: Path,
    workspace_id: str,
    agent_id: str,
    skill_id: str,
    match: str,
) -> None:
    _store_obj, memory = _store(tmp_path)
    candidate = _candidate()
    verification = _verification(candidate)
    payload = b'{"procedure":"bound"}'
    intent = _intent(candidate=candidate, verification=verification, payload=payload)

    with pytest.raises(ValueError, match=match):
        _apply(
            LearningSkillApplier(LearnedSkillService(memory)),
            intent=intent,
            candidate=candidate,
            verification=verification,
            payload=payload,
            workspace_id=workspace_id,
            agent_id=agent_id,
            skill_id=skill_id,
        )


@pytest.mark.parametrize(
    "payload",
    (
        b'{"x":1,"x":2}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":1e999}',
        b'{"x":',
        b"\xff",
    ),
)
def test_skill_payload_uses_shared_strict_json_before_effect(
    tmp_path: Path,
    payload: bytes,
) -> None:
    _store_obj, memory = _store(tmp_path)
    candidate = _candidate()
    verification = _verification(candidate)
    intent = _intent(candidate=candidate, verification=verification, payload=payload)
    skills = LearnedSkillService(memory)

    with pytest.raises(ValueError):
        _apply(
            LearningSkillApplier(skills),
            intent=intent,
            candidate=candidate,
            verification=verification,
            payload=payload,
        )
    assert skills.get(
        workspace_id="workspace-1",
        agent_id="agent-1",
        skill_id="document-triage",
    ) is None


@pytest.mark.parametrize(
    ("target", "schema", "match"),
    (
        (LearningUpdateTarget.WORLD_MODEL, SKILL_UPDATE_SCHEMA, "only SKILL"),
        (LearningUpdateTarget.SKILL, "other.schema/v1", "unsupported"),
    ),
)
def test_wrong_target_or_schema_fails_before_skill_effect(
    tmp_path: Path,
    target: LearningUpdateTarget,
    schema: str,
    match: str,
) -> None:
    _store_obj, memory = _store(tmp_path)
    candidate = _candidate()
    verification = _verification(candidate)
    payload = b'{"procedure":"blocked"}'
    intent = _intent(
        candidate=candidate,
        verification=verification,
        payload=payload,
        target=target,
        schema=schema,
    )
    skills = LearnedSkillService(memory)

    with pytest.raises(ValueError, match=match):
        _apply(
            LearningSkillApplier(skills),
            intent=intent,
            candidate=candidate,
            verification=verification,
            payload=payload,
        )
    assert skills.get(
        workspace_id="workspace-1",
        agent_id="agent-1",
        skill_id="document-triage",
    ) is None


def test_forged_intent_is_revalidated_before_skill_effect(tmp_path: Path) -> None:
    _store_obj, memory = _store(tmp_path)
    candidate = _candidate()
    verification = _verification(candidate)
    payload = b'{"procedure":"bounded"}'
    good = _intent(candidate=candidate, verification=verification, payload=payload)
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
    skills = LearnedSkillService(memory)

    with pytest.raises(ValueError, match="trusted cognition"):
        _apply(
            LearningSkillApplier(skills),
            intent=forged,
            candidate=candidate,
            verification=verification,
            payload=payload,
        )
    assert skills.get(
        workspace_id="workspace-1",
        agent_id="agent-1",
        skill_id="document-triage",
    ) is None


def test_skill_target_identity_is_deterministic_bounded_and_agent_bound() -> None:
    first = learned_skill_target_ref_sha256(
        workspace_id="workspace-1",
        agent_id="agent-1",
        skill_id="document-triage",
    )
    assert first == learned_skill_target_ref_sha256(
        workspace_id="workspace-1",
        agent_id="agent-1",
        skill_id="document-triage",
    )
    assert first != learned_skill_target_ref_sha256(
        workspace_id="workspace-1",
        agent_id="agent-2",
        skill_id="document-triage",
    )
    with pytest.raises(ValueError, match="bounded machine token"):
        learned_skill_target_ref_sha256(
            workspace_id="workspace-1",
            agent_id="agent-1",
            skill_id="bad skill",
        )
