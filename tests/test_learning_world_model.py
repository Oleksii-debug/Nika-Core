from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.learning_cognition import (
    CognitionCandidate,
    CognitionCandidateKind,
    CognitionEvidenceRef,
    CognitionVerification,
    CognitionVerificationCheck,
    CognitionVerificationRequirement,
)
from nika_core.learning_update import LearningUpdateIntent, LearningUpdateTarget
from nika_core.learning_world_model import (
    WORLD_MODEL_UPDATE_SCHEMA,
    LearningWorldModelApplier,
)
from nika_core.memory.contracts import MemoryConflictError, MemoryScope
from nika_core.memory.service import MemoryService
from nika_core.world_model import (
    WORLD_MODEL_NAMESPACE,
    WorldModelService,
    world_model_target_ref_sha256,
)

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
    candidate_id: str = "candidate-world-1",
    workspace_id: str = "workspace-1",
    agent_id: str = "agent-1",
) -> CognitionCandidate:
    return CognitionCandidate.create(
        candidate_id=candidate_id,
        workspace_id=workspace_id,
        agent_id=agent_id,
        kind=CognitionCandidateKind.HYPOTHESIS,
        statement="verified bounded world-state update",
        evidence=(
            CognitionEvidenceRef(
                source_type="comparison",
                source_id="comparison-world-1",
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


def _memory(tmp_path: Path) -> MemoryService:
    store = SQLiteStore(tmp_path / "world model" / "nika.db")
    store.initialize()
    return MemoryService(store)


def _intent(
    *,
    candidate: CognitionCandidate,
    verification: CognitionVerification,
    payload: bytes,
    topic: str = "environment",
    target: LearningUpdateTarget = LearningUpdateTarget.WORLD_MODEL,
    schema: str = WORLD_MODEL_UPDATE_SCHEMA,
    target_ref_sha256: str | None = None,
    expected_revision_sha256: str | None = None,
) -> LearningUpdateIntent:
    return LearningUpdateIntent.bind_payload(
        intent_id=f"world-update-{candidate.candidate_id}",
        candidate=candidate,
        verification=verification,
        expected_verification_policy_sha256=POLICY,
        expected_requirements=_requirements(),
        target=target,
        target_ref_sha256=target_ref_sha256
        or world_model_target_ref_sha256(
            workspace_id=candidate.workspace_id,
            topic=topic,
        ),
        update_schema=schema,
        payload=payload,
        expected_revision_sha256=expected_revision_sha256,
    )


def _apply(
    applier: LearningWorldModelApplier,
    *,
    intent: LearningUpdateIntent,
    candidate: CognitionCandidate,
    verification: CognitionVerification,
    payload: bytes,
    workspace_id: str = "workspace-1",
    topic: str = "environment",
):
    return applier.apply(
        intent=intent,
        candidate=candidate,
        verification=verification,
        expected_verification_policy_sha256=POLICY,
        expected_requirements=_requirements(),
        payload=payload,
        workspace_id=workspace_id,
        topic=topic,
    )


def test_verified_world_model_create_uses_workspace_scope_and_minimization(
    tmp_path: Path,
) -> None:
    memory = _memory(tmp_path)
    world = WorldModelService(memory)
    candidate = _candidate()
    verification = _verification(candidate)
    secret = "sk-world-model-secret"
    payload = ('{"weather":"rain","api_key":"' + secret + '"}').encode()
    intent = _intent(candidate=candidate, verification=verification, payload=payload)

    receipt = _apply(
        LearningWorldModelApplier(world),
        intent=intent,
        candidate=candidate,
        verification=verification,
        payload=payload,
    )
    snapshot = world.get(workspace_id="workspace-1", topic="environment")
    record = memory.get(
        scope=MemoryScope.WORKSPACE,
        owner_id="workspace-1",
        namespace=WORLD_MODEL_NAMESPACE,
        key="environment",
    )

    assert snapshot is not None
    assert record is not None
    assert snapshot.value == {"api_key": "[REDACTED]", "weather": "rain"}
    assert snapshot.value == record.value
    assert receipt.created is True
    assert receipt.target_ref_sha256 == intent.target_ref_sha256
    assert receipt.revision_sha256 == snapshot.revision_sha256
    assert secret not in repr(receipt)


def test_world_model_is_workspace_shared_across_verified_agents(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    world = WorldModelService(memory)
    first_candidate = _candidate(agent_id="agent-a")
    first_verification = _verification(first_candidate)
    first_payload = b'{"state":"one"}'
    first_intent = _intent(
        candidate=first_candidate,
        verification=first_verification,
        payload=first_payload,
    )
    first = _apply(
        LearningWorldModelApplier(world),
        intent=first_intent,
        candidate=first_candidate,
        verification=first_verification,
        payload=first_payload,
    )

    second_candidate = _candidate(
        candidate_id="candidate-world-2",
        agent_id="agent-b",
    )
    second_verification = _verification(second_candidate)
    second_payload = b'{"state":"two"}'
    second_intent = _intent(
        candidate=second_candidate,
        verification=second_verification,
        payload=second_payload,
        expected_revision_sha256=first.revision_sha256,
    )
    second = _apply(
        LearningWorldModelApplier(world),
        intent=second_intent,
        candidate=second_candidate,
        verification=second_verification,
        payload=second_payload,
    )

    assert second.created is False
    assert second.target_ref_sha256 == first.target_ref_sha256
    snapshot = world.get(workspace_id="workspace-1", topic="environment")
    assert snapshot is not None and snapshot.value == {"state": "two"}


def test_missing_revision_is_create_only(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    world = WorldModelService(memory)
    world.compare_and_put(
        workspace_id="workspace-1",
        topic="environment",
        value={"generation": 1},
        expected_revision_sha256=None,
    )
    candidate = _candidate()
    verification = _verification(candidate)
    payload = b'{"generation":2}'
    intent = _intent(candidate=candidate, verification=verification, payload=payload)

    with pytest.raises(MemoryConflictError):
        _apply(
            LearningWorldModelApplier(world),
            intent=intent,
            candidate=candidate,
            verification=verification,
            payload=payload,
        )

    snapshot = world.get(workspace_id="workspace-1", topic="environment")
    assert snapshot is not None and snapshot.value == {"generation": 1}


def test_stale_revision_preserves_newer_world_state(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    world = WorldModelService(memory)
    original = world.compare_and_put(
        workspace_id="workspace-1",
        topic="environment",
        value={"generation": 1},
        expected_revision_sha256=None,
    )
    winner = world.compare_and_put(
        workspace_id="workspace-1",
        topic="environment",
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
            LearningWorldModelApplier(world),
            intent=intent,
            candidate=candidate,
            verification=verification,
            payload=payload,
        )

    assert world.get(workspace_id="workspace-1", topic="environment") == winner


def test_workspace_and_topic_cannot_be_rebound_after_intent_binding(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    world = WorldModelService(memory)
    candidate = _candidate()
    verification = _verification(candidate)
    payload = b'{"state":"bound"}'
    intent = _intent(candidate=candidate, verification=verification, payload=payload)

    with pytest.raises(ValueError, match="workspace"):
        _apply(
            LearningWorldModelApplier(world),
            intent=intent,
            candidate=candidate,
            verification=verification,
            payload=payload,
            workspace_id="workspace-2",
        )
    with pytest.raises(ValueError, match="target does not match"):
        _apply(
            LearningWorldModelApplier(world),
            intent=intent,
            candidate=candidate,
            verification=verification,
            payload=payload,
            topic="other",
        )
    assert world.get(workspace_id="workspace-1", topic="environment") is None


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
def test_world_model_payload_uses_shared_strict_json_before_effect(
    tmp_path: Path,
    payload: bytes,
) -> None:
    memory = _memory(tmp_path)
    world = WorldModelService(memory)
    candidate = _candidate()
    verification = _verification(candidate)
    intent = _intent(candidate=candidate, verification=verification, payload=payload)

    with pytest.raises(ValueError):
        _apply(
            LearningWorldModelApplier(world),
            intent=intent,
            candidate=candidate,
            verification=verification,
            payload=payload,
        )

    assert world.get(workspace_id="workspace-1", topic="environment") is None


@pytest.mark.parametrize(
    ("target", "schema", "match"),
    (
        (LearningUpdateTarget.SKILL, WORLD_MODEL_UPDATE_SCHEMA, "only WORLD_MODEL"),
        (LearningUpdateTarget.WORLD_MODEL, "other.schema/v1", "unsupported"),
    ),
)
def test_wrong_target_or_schema_fails_before_world_model_effect(
    tmp_path: Path,
    target: LearningUpdateTarget,
    schema: str,
    match: str,
) -> None:
    memory = _memory(tmp_path)
    candidate = _candidate()
    verification = _verification(candidate)
    payload = b'{"state":"blocked"}'
    intent = _intent(
        candidate=candidate,
        verification=verification,
        payload=payload,
        target=target,
        schema=schema,
    )

    with pytest.raises(ValueError, match=match):
        _apply(
            LearningWorldModelApplier(WorldModelService(memory)),
            intent=intent,
            candidate=candidate,
            verification=verification,
            payload=payload,
        )
    assert WorldModelService(memory).get(
        workspace_id="workspace-1",
        topic="environment",
    ) is None


def test_forged_intent_is_revalidated_before_world_model_effect(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    candidate = _candidate()
    verification = _verification(candidate)
    payload = b'{"state":"bounded"}'
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

    with pytest.raises(ValueError, match="trusted cognition"):
        _apply(
            LearningWorldModelApplier(WorldModelService(memory)),
            intent=forged,
            candidate=candidate,
            verification=verification,
            payload=payload,
        )
    assert WorldModelService(memory).get(
        workspace_id="workspace-1",
        topic="environment",
    ) is None


def test_world_model_target_identity_is_deterministic_and_bounded() -> None:
    first = world_model_target_ref_sha256(
        workspace_id="workspace-1",
        topic="environment",
    )
    assert first == world_model_target_ref_sha256(
        workspace_id="workspace-1",
        topic="environment",
    )
    assert first != world_model_target_ref_sha256(
        workspace_id="workspace-1",
        topic="other",
    )
    with pytest.raises(ValueError, match="bounded machine token"):
        world_model_target_ref_sha256(
            workspace_id="workspace-1",
            topic="bad topic",
        )
