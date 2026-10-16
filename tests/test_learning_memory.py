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
from nika_core.learning_memory import (
    MEMORY_UPDATE_SCHEMA,
    LearningMemoryApplier,
    memory_revision_sha256,
    memory_target_ref_sha256,
)
from nika_core.learning_update import LearningUpdateIntent, LearningUpdateTarget
from nika_core.memory.contracts import MemoryConflictError, MemoryScope
from nika_core.memory.service import MemoryService

A = "a" * 64
B = "b" * 64
C = "c" * 64
POLICY = A
VERIFIER = B
SOURCE_EVIDENCE = C


def _memory(tmp_path: Path) -> MemoryService:
    store = SQLiteStore(tmp_path / "Ніка навчання" / "nika.db")
    store.initialize()
    return MemoryService(store)


def _requirements() -> tuple[CognitionVerificationRequirement, ...]:
    return (
        CognitionVerificationRequirement(
            check_id="semantic-truth",
            verifier_sha256=VERIFIER,
        ),
    )


def _candidate(
    *,
    workspace_id: str = "workspace-1",
    agent_id: str = "agent-1",
) -> CognitionCandidate:
    return CognitionCandidate.create(
        candidate_id="candidate-1",
        workspace_id=workspace_id,
        agent_id=agent_id,
        kind=CognitionCandidateKind.HYPOTHESIS,
        statement="verified bounded semantic update",
        evidence=(
            CognitionEvidenceRef(
                source_type="comparison",
                source_id="comparison-1",
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


def _target_ref(
    *,
    scope: MemoryScope = MemoryScope.WORKSPACE,
    owner_id: str = "workspace-1",
    namespace: str = "learned",
    key: str = "fact-1",
) -> str:
    return memory_target_ref_sha256(
        scope=scope,
        owner_id=owner_id,
        namespace=namespace,
        key=key,
    )


def _intent(
    *,
    candidate: CognitionCandidate,
    verification: CognitionVerification,
    payload: bytes,
    target_ref_sha256: str | None = None,
    target: LearningUpdateTarget = LearningUpdateTarget.MEMORY,
    schema: str = MEMORY_UPDATE_SCHEMA,
    expected_revision_sha256: str | None = None,
) -> LearningUpdateIntent:
    return LearningUpdateIntent.bind_payload(
        intent_id="memory-update-1",
        candidate=candidate,
        verification=verification,
        expected_verification_policy_sha256=POLICY,
        expected_requirements=_requirements(),
        target=target,
        target_ref_sha256=target_ref_sha256 or _target_ref(),
        update_schema=schema,
        payload=payload,
        expected_revision_sha256=expected_revision_sha256,
    )


def _apply(
    applier: LearningMemoryApplier,
    *,
    intent: LearningUpdateIntent,
    candidate: CognitionCandidate,
    verification: CognitionVerification,
    payload: bytes,
    scope: MemoryScope = MemoryScope.WORKSPACE,
    owner_id: str = "workspace-1",
    namespace: str = "learned",
    key: str = "fact-1",
):
    return applier.apply(
        intent=intent,
        candidate=candidate,
        verification=verification,
        expected_verification_policy_sha256=POLICY,
        expected_requirements=_requirements(),
        payload=payload,
        scope=scope,
        owner_id=owner_id,
        namespace=namespace,
        key=key,
    )


def test_verified_create_uses_memory_service_minimization_and_private_receipt(
    tmp_path: Path,
) -> None:
    memory = _memory(tmp_path)
    applier = LearningMemoryApplier(memory)
    candidate = _candidate()
    verification = _verification(candidate)
    secret = "sk-learning-memory-secret"
    payload = (
        '{"fact":"bounded","api_key":"' + secret + '"}'
    ).encode("utf-8")
    intent = _intent(
        candidate=candidate,
        verification=verification,
        payload=payload,
    )

    receipt = _apply(
        applier,
        intent=intent,
        candidate=candidate,
        verification=verification,
        payload=payload,
    )

    record = memory.get(
        scope=MemoryScope.WORKSPACE,
        owner_id="workspace-1",
        namespace="learned",
        key="fact-1",
    )
    assert record is not None
    assert record.value == {"api_key": "[REDACTED]", "fact": "bounded"}
    assert receipt.created is True
    assert receipt.target_ref_sha256 == intent.target_ref_sha256
    assert receipt.revision_sha256 == memory_revision_sha256(record)
    assert secret not in repr(receipt)


def test_agent_scope_must_bind_exact_candidate_agent(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    applier = LearningMemoryApplier(memory)
    candidate = _candidate(agent_id="agent-a")
    verification = _verification(candidate)
    payload = b'{"fact":"agent"}'
    target = _target_ref(
        scope=MemoryScope.AGENT,
        owner_id="agent-a",
    )
    intent = _intent(
        candidate=candidate,
        verification=verification,
        payload=payload,
        target_ref_sha256=target,
    )

    receipt = _apply(
        applier,
        intent=intent,
        candidate=candidate,
        verification=verification,
        payload=payload,
        scope=MemoryScope.AGENT,
        owner_id="agent-a",
    )

    assert receipt.created is True
    assert memory.get(
        scope=MemoryScope.AGENT,
        owner_id="agent-a",
        namespace="learned",
        key="fact-1",
    ) is not None


@pytest.mark.parametrize("scope", [MemoryScope.USER, MemoryScope.TASK])
def test_self_learning_cannot_write_user_or_task_scope(
    tmp_path: Path,
    scope: MemoryScope,
) -> None:
    memory = _memory(tmp_path)
    candidate = _candidate()
    verification = _verification(candidate)
    payload = b'{"fact":"blocked"}'
    intent = _intent(
        candidate=candidate,
        verification=verification,
        payload=payload,
    )

    with pytest.raises(ValueError, match="AGENT or WORKSPACE"):
        _apply(
            LearningMemoryApplier(memory),
            intent=intent,
            candidate=candidate,
            verification=verification,
            payload=payload,
            scope=scope,
            owner_id="workspace-1",
        )


def test_target_address_hash_prevents_rebinding_to_other_memory_key(
    tmp_path: Path,
) -> None:
    memory = _memory(tmp_path)
    candidate = _candidate()
    verification = _verification(candidate)
    payload = b'{"fact":"bounded"}'
    intent = _intent(
        candidate=candidate,
        verification=verification,
        payload=payload,
    )

    with pytest.raises(ValueError, match="target does not match"):
        _apply(
            LearningMemoryApplier(memory),
            intent=intent,
            candidate=candidate,
            verification=verification,
            payload=payload,
            key="other-key",
        )

    assert memory.get(
        scope=MemoryScope.WORKSPACE,
        owner_id="workspace-1",
        namespace="learned",
        key="other-key",
    ) is None


def test_workspace_owner_must_match_cognition_scope_before_write(
    tmp_path: Path,
) -> None:
    memory = _memory(tmp_path)
    candidate = _candidate(workspace_id="workspace-a")
    verification = _verification(candidate)
    payload = b'{"fact":"blocked"}'
    target = _target_ref(owner_id="workspace-b")
    intent = _intent(
        candidate=candidate,
        verification=verification,
        payload=payload,
        target_ref_sha256=target,
    )

    with pytest.raises(ValueError, match="owner does not match"):
        _apply(
            LearningMemoryApplier(memory),
            intent=intent,
            candidate=candidate,
            verification=verification,
            payload=payload,
            owner_id="workspace-b",
        )


def test_missing_revision_means_create_only(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    memory.compare_and_put(
        scope=MemoryScope.WORKSPACE,
        owner_id="workspace-1",
        namespace="learned",
        key="fact-1",
        value={"generation": 1},
        expected_updated_at=None,
    )
    candidate = _candidate()
    verification = _verification(candidate)
    payload = b'{"generation":2}'
    intent = _intent(
        candidate=candidate,
        verification=verification,
        payload=payload,
    )

    with pytest.raises(MemoryConflictError):
        _apply(
            LearningMemoryApplier(memory),
            intent=intent,
            candidate=candidate,
            verification=verification,
            payload=payload,
        )

    assert memory.get(
        scope=MemoryScope.WORKSPACE,
        owner_id="workspace-1",
        namespace="learned",
        key="fact-1",
    ).value == {"generation": 1}


def test_exact_revision_digest_allows_conditional_update(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    original = memory.compare_and_put(
        scope=MemoryScope.WORKSPACE,
        owner_id="workspace-1",
        namespace="learned",
        key="fact-1",
        value={"generation": 1},
        expected_updated_at=None,
    )
    candidate = _candidate()
    verification = _verification(candidate)
    payload = b'{"generation":2}'
    intent = _intent(
        candidate=candidate,
        verification=verification,
        payload=payload,
        expected_revision_sha256=memory_revision_sha256(original),
    )

    receipt = _apply(
        LearningMemoryApplier(memory),
        intent=intent,
        candidate=candidate,
        verification=verification,
        payload=payload,
    )

    updated = memory.get(
        scope=MemoryScope.WORKSPACE,
        owner_id="workspace-1",
        namespace="learned",
        key="fact-1",
    )
    assert updated is not None and updated.value == {"generation": 2}
    assert receipt.created is False
    assert receipt.revision_sha256 == memory_revision_sha256(updated)


def test_stale_revision_digest_preserves_newer_winner(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    original = memory.compare_and_put(
        scope=MemoryScope.WORKSPACE,
        owner_id="workspace-1",
        namespace="learned",
        key="fact-1",
        value={"generation": 1},
        expected_updated_at=None,
    )
    stale_revision = memory_revision_sha256(original)
    winner = memory.compare_and_put(
        scope=MemoryScope.WORKSPACE,
        owner_id="workspace-1",
        namespace="learned",
        key="fact-1",
        value={"generation": 2},
        expected_updated_at=original.updated_at,
    )
    candidate = _candidate()
    verification = _verification(candidate)
    payload = b'{"generation":3}'
    intent = _intent(
        candidate=candidate,
        verification=verification,
        payload=payload,
        expected_revision_sha256=stale_revision,
    )

    with pytest.raises(MemoryConflictError, match="revision changed"):
        _apply(
            LearningMemoryApplier(memory),
            intent=intent,
            candidate=candidate,
            verification=verification,
            payload=payload,
        )

    assert memory.get(
        scope=MemoryScope.WORKSPACE,
        owner_id="workspace-1",
        namespace="learned",
        key="fact-1",
    ) == winner


@pytest.mark.parametrize(
    "payload",
    [
        b'{"x":1,"x":2}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":1e999}',
        b'{"x":',
        b"\xff",
    ],
)
def test_semantic_payload_must_be_strict_utf8_json_before_memory_effect(
    tmp_path: Path,
    payload: bytes,
) -> None:
    memory = _memory(tmp_path)
    candidate = _candidate()
    verification = _verification(candidate)
    intent = _intent(
        candidate=candidate,
        verification=verification,
        payload=payload,
    )

    with pytest.raises(ValueError):
        _apply(
            LearningMemoryApplier(memory),
            intent=intent,
            candidate=candidate,
            verification=verification,
            payload=payload,
        )

    assert memory.get(
        scope=MemoryScope.WORKSPACE,
        owner_id="workspace-1",
        namespace="learned",
        key="fact-1",
    ) is None


def test_payload_substitution_is_rejected_before_memory_effect(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    candidate = _candidate()
    verification = _verification(candidate)
    intent = _intent(
        candidate=candidate,
        verification=verification,
        payload=b'{"fact":"original"}',
    )

    with pytest.raises(ValueError, match="trusted cognition"):
        _apply(
            LearningMemoryApplier(memory),
            intent=intent,
            candidate=candidate,
            verification=verification,
            payload=b'{"fact":"changed!"}',
        )


@pytest.mark.parametrize(
    ("target", "schema", "match"),
    [
        (LearningUpdateTarget.SKILL, MEMORY_UPDATE_SCHEMA, "only MEMORY"),
        (LearningUpdateTarget.MEMORY, "other.schema/v1", "unsupported"),
    ],
)
def test_adapter_rejects_wrong_target_or_schema_before_mutation(
    tmp_path: Path,
    target: LearningUpdateTarget,
    schema: str,
    match: str,
) -> None:
    memory = _memory(tmp_path)
    candidate = _candidate()
    verification = _verification(candidate)
    payload = b'{"fact":"blocked"}'
    intent = _intent(
        candidate=candidate,
        verification=verification,
        payload=payload,
        target=target,
        schema=schema,
    )

    with pytest.raises(ValueError, match=match):
        _apply(
            LearningMemoryApplier(memory),
            intent=intent,
            candidate=candidate,
            verification=verification,
            payload=payload,
        )


def test_target_and_revision_hashes_are_deterministic_and_target_bound(
    tmp_path: Path,
) -> None:
    first = _target_ref()
    assert first == _target_ref()
    assert first != _target_ref(key="fact-2")

    memory = _memory(tmp_path)
    one = memory.compare_and_put(
        scope=MemoryScope.WORKSPACE,
        owner_id="workspace-1",
        namespace="learned",
        key="fact-1",
        value={"v": 1},
        expected_updated_at=None,
    )
    other = memory.compare_and_put(
        scope=MemoryScope.WORKSPACE,
        owner_id="workspace-1",
        namespace="learned",
        key="fact-2",
        value={"v": 1},
        expected_updated_at=None,
    )
    assert memory_revision_sha256(one) != memory_revision_sha256(other)


def test_forged_exact_intent_is_revalidated_before_target_effect(tmp_path: Path) -> None:
    memory = _memory(tmp_path)
    candidate = _candidate()
    verification = _verification(candidate)
    payload = b'{"fact":"bounded"}'
    good = _intent(
        candidate=candidate,
        verification=verification,
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
            LearningMemoryApplier(memory),
            intent=forged,
            candidate=candidate,
            verification=verification,
            payload=payload,
        )


def test_target_identity_rejects_ambiguous_text() -> None:
    with pytest.raises(ValueError, match="surrounding whitespace"):
        memory_target_ref_sha256(
            scope=MemoryScope.WORKSPACE,
            owner_id=" workspace-1",
            namespace="learned",
            key="fact-1",
        )
    with pytest.raises(ValueError, match="NFC"):
        memory_target_ref_sha256(
            scope=MemoryScope.WORKSPACE,
            owner_id="workspace-1",
            namespace="cafe\u0301",
            key="fact-1",
        )
