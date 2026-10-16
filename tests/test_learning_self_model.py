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
from nika_core.learning_self_model import (
    SELF_MODEL_UPDATE_SCHEMA,
    LearningSelfModelApplier,
)
from nika_core.learning_update import LearningUpdateIntent, LearningUpdateTarget
from nika_core.memory.contracts import MemoryConflictError, MemoryScope
from nika_core.memory.service import MemoryService
from nika_core.self_model import (
    SELF_MODEL_NAMESPACE,
    SelfModelService,
    self_model_target_ref_sha256,
)

A = "a" * 64
B = "b" * 64
C = "c" * 64
POLICY = A
VERIFIER = B
SOURCE_EVIDENCE = C


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "Ніка self model" / "nika.db")
    store.initialize()
    return store


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
        statement="verified bounded self-model update",
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
    workspace_id: str = "workspace-1",
    agent_id: str = "agent-1",
    facet: str = "capabilities",
) -> str:
    return self_model_target_ref_sha256(
        workspace_id=workspace_id,
        agent_id=agent_id,
        facet=facet,
    )


def _intent(
    *,
    candidate: CognitionCandidate,
    verification: CognitionVerification,
    payload: bytes,
    target_ref_sha256: str | None = None,
    target: LearningUpdateTarget = LearningUpdateTarget.SELF_MODEL,
    schema: str = SELF_MODEL_UPDATE_SCHEMA,
    expected_revision_sha256: str | None = None,
) -> LearningUpdateIntent:
    return LearningUpdateIntent.bind_payload(
        intent_id="self-model-update-1",
        candidate=candidate,
        verification=verification,
        expected_verification_policy_sha256=POLICY,
        expected_requirements=_requirements(),
        target=target,
        target_ref_sha256=target_ref_sha256
        or _target_ref(
            workspace_id=candidate.workspace_id,
            agent_id=candidate.agent_id,
        ),
        update_schema=schema,
        payload=payload,
        expected_revision_sha256=expected_revision_sha256,
    )


def _apply(
    applier: LearningSelfModelApplier,
    *,
    intent: LearningUpdateIntent,
    candidate: CognitionCandidate,
    verification: CognitionVerification,
    payload: bytes,
    workspace_id: str = "workspace-1",
    agent_id: str = "agent-1",
    facet: str = "capabilities",
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
        facet=facet,
    )


def test_verified_create_uses_memory_minimization_and_private_receipt(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    memory = MemoryService(store)
    self_model = SelfModelService(memory)
    candidate = _candidate()
    verification = _verification(candidate)
    secret = "sk-self-model-secret"
    payload = (
        '{"strength":"planning","api_key":"' + secret + '"}'
    ).encode("utf-8")
    intent = _intent(
        candidate=candidate,
        verification=verification,
        payload=payload,
    )

    receipt = _apply(
        LearningSelfModelApplier(self_model),
        intent=intent,
        candidate=candidate,
        verification=verification,
        payload=payload,
    )

    snapshot = self_model.get(
        workspace_id="workspace-1",
        agent_id="agent-1",
        facet="capabilities",
    )
    assert snapshot is not None
    assert snapshot.value == {
        "api_key": "[REDACTED]",
        "strength": "planning",
    }
    assert receipt.created is True
    assert receipt.target_ref_sha256 == intent.target_ref_sha256
    assert receipt.revision_sha256 == snapshot.revision_sha256
    assert secret not in repr(receipt)


def test_self_model_is_workspace_agent_and_facet_bound(tmp_path: Path) -> None:
    service = SelfModelService(MemoryService(_store(tmp_path)))

    first = service.compare_and_put(
        workspace_id="workspace-a",
        agent_id="agent-1",
        facet="capabilities",
        value={"v": "a"},
        expected_revision_sha256=None,
    )
    second = service.compare_and_put(
        workspace_id="workspace-b",
        agent_id="agent-1",
        facet="capabilities",
        value={"v": "b"},
        expected_revision_sha256=None,
    )
    third = service.compare_and_put(
        workspace_id="workspace-a",
        agent_id="agent-1",
        facet="limits",
        value={"v": "c"},
        expected_revision_sha256=None,
    )

    assert first.target_ref_sha256 != second.target_ref_sha256
    assert first.target_ref_sha256 != third.target_ref_sha256
    assert service.get(
        workspace_id="workspace-a",
        agent_id="agent-1",
        facet="capabilities",
    ).value == {"v": "a"}
    assert service.get(
        workspace_id="workspace-b",
        agent_id="agent-1",
        facet="capabilities",
    ).value == {"v": "b"}


@pytest.mark.parametrize(
    ("workspace_id", "agent_id", "match"),
    [
        ("workspace-other", "agent-1", "workspace"),
        ("workspace-1", "agent-other", "agent"),
    ],
)
def test_cognition_scope_cannot_be_rebound_before_self_model_effect(
    tmp_path: Path,
    workspace_id: str,
    agent_id: str,
    match: str,
) -> None:
    service = SelfModelService(MemoryService(_store(tmp_path)))
    candidate = _candidate()
    verification = _verification(candidate)
    payload = b'{"strength":"bounded"}'
    intent = _intent(
        candidate=candidate,
        verification=verification,
        payload=payload,
    )

    with pytest.raises(ValueError, match=match):
        _apply(
            LearningSelfModelApplier(service),
            intent=intent,
            candidate=candidate,
            verification=verification,
            payload=payload,
            workspace_id=workspace_id,
            agent_id=agent_id,
        )

    assert service.get(
        workspace_id="workspace-1",
        agent_id="agent-1",
        facet="capabilities",
    ) is None


def test_target_ref_prevents_rebinding_to_other_facet(tmp_path: Path) -> None:
    service = SelfModelService(MemoryService(_store(tmp_path)))
    candidate = _candidate()
    verification = _verification(candidate)
    payload = b'{"strength":"bounded"}'
    intent = _intent(
        candidate=candidate,
        verification=verification,
        payload=payload,
    )

    with pytest.raises(ValueError, match="target does not match"):
        _apply(
            LearningSelfModelApplier(service),
            intent=intent,
            candidate=candidate,
            verification=verification,
            payload=payload,
            facet="limits",
        )

    assert service.get(
        workspace_id="workspace-1",
        agent_id="agent-1",
        facet="limits",
    ) is None


def test_missing_revision_is_create_only(tmp_path: Path) -> None:
    service = SelfModelService(MemoryService(_store(tmp_path)))
    service.compare_and_put(
        workspace_id="workspace-1",
        agent_id="agent-1",
        facet="capabilities",
        value={"generation": 1},
        expected_revision_sha256=None,
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
            LearningSelfModelApplier(service),
            intent=intent,
            candidate=candidate,
            verification=verification,
            payload=payload,
        )

    assert service.get(
        workspace_id="workspace-1",
        agent_id="agent-1",
        facet="capabilities",
    ).value == {"generation": 1}


def test_exact_revision_allows_conditional_update(tmp_path: Path) -> None:
    service = SelfModelService(MemoryService(_store(tmp_path)))
    original = service.compare_and_put(
        workspace_id="workspace-1",
        agent_id="agent-1",
        facet="capabilities",
        value={"generation": 1},
        expected_revision_sha256=None,
    )
    candidate = _candidate()
    verification = _verification(candidate)
    payload = b'{"generation":2}'
    intent = _intent(
        candidate=candidate,
        verification=verification,
        payload=payload,
        expected_revision_sha256=original.revision_sha256,
    )

    receipt = _apply(
        LearningSelfModelApplier(service),
        intent=intent,
        candidate=candidate,
        verification=verification,
        payload=payload,
    )

    updated = service.get(
        workspace_id="workspace-1",
        agent_id="agent-1",
        facet="capabilities",
    )
    assert updated is not None
    assert updated.value == {"generation": 2}
    assert receipt.created is False
    assert receipt.revision_sha256 == updated.revision_sha256
    assert receipt.revision_sha256 != original.revision_sha256


def test_stale_revision_preserves_newer_winner(tmp_path: Path) -> None:
    service = SelfModelService(MemoryService(_store(tmp_path)))
    original = service.compare_and_put(
        workspace_id="workspace-1",
        agent_id="agent-1",
        facet="capabilities",
        value={"generation": 1},
        expected_revision_sha256=None,
    )
    winner = service.compare_and_put(
        workspace_id="workspace-1",
        agent_id="agent-1",
        facet="capabilities",
        value={"generation": 2},
        expected_revision_sha256=original.revision_sha256,
    )
    candidate = _candidate()
    verification = _verification(candidate)
    payload = b'{"generation":3}'
    stale = _intent(
        candidate=candidate,
        verification=verification,
        payload=payload,
        expected_revision_sha256=original.revision_sha256,
    )

    with pytest.raises(MemoryConflictError, match="revision changed"):
        _apply(
            LearningSelfModelApplier(service),
            intent=stale,
            candidate=candidate,
            verification=verification,
            payload=payload,
        )

    assert service.get(
        workspace_id="workspace-1",
        agent_id="agent-1",
        facet="capabilities",
    ) == winner


def test_restart_reopens_same_self_model_revision_without_new_schema(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    first_service = SelfModelService(MemoryService(store))
    first = first_service.compare_and_put(
        workspace_id="workspace-1",
        agent_id="agent-1",
        facet="capabilities",
        value={"generation": 1},
        expected_revision_sha256=None,
    )

    reopened = SelfModelService(MemoryService(store)).get(
        workspace_id="workspace-1",
        agent_id="agent-1",
        facet="capabilities",
    )

    assert reopened == first
    with store.connection() as conn:
        self_model_tables = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name LIKE '%self_model%'"
        ).fetchall()
    assert self_model_tables == []


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
def test_strict_learning_json_rejects_ambiguous_payload_before_effect(
    tmp_path: Path,
    payload: bytes,
) -> None:
    service = SelfModelService(MemoryService(_store(tmp_path)))
    candidate = _candidate()
    verification = _verification(candidate)
    intent = _intent(
        candidate=candidate,
        verification=verification,
        payload=payload,
    )

    with pytest.raises(ValueError):
        _apply(
            LearningSelfModelApplier(service),
            intent=intent,
            candidate=candidate,
            verification=verification,
            payload=payload,
        )

    assert service.get(
        workspace_id="workspace-1",
        agent_id="agent-1",
        facet="capabilities",
    ) is None


def test_payload_substitution_is_rejected_before_self_model_effect(
    tmp_path: Path,
) -> None:
    service = SelfModelService(MemoryService(_store(tmp_path)))
    candidate = _candidate()
    verification = _verification(candidate)
    intent = _intent(
        candidate=candidate,
        verification=verification,
        payload=b'{"strength":"original"}',
    )

    with pytest.raises(ValueError, match="trusted cognition"):
        _apply(
            LearningSelfModelApplier(service),
            intent=intent,
            candidate=candidate,
            verification=verification,
            payload=b'{"strength":"changed!"}',
        )


@pytest.mark.parametrize(
    ("target", "schema", "match"),
    [
        (LearningUpdateTarget.MEMORY, SELF_MODEL_UPDATE_SCHEMA, "only SELF_MODEL"),
        (LearningUpdateTarget.SELF_MODEL, "other.schema/v1", "unsupported"),
    ],
)
def test_adapter_rejects_wrong_target_or_schema_before_mutation(
    tmp_path: Path,
    target: LearningUpdateTarget,
    schema: str,
    match: str,
) -> None:
    service = SelfModelService(MemoryService(_store(tmp_path)))
    candidate = _candidate()
    verification = _verification(candidate)
    payload = b'{"strength":"blocked"}'
    intent = _intent(
        candidate=candidate,
        verification=verification,
        payload=payload,
        target=target,
        schema=schema,
    )

    with pytest.raises(ValueError, match=match):
        _apply(
            LearningSelfModelApplier(service),
            intent=intent,
            candidate=candidate,
            verification=verification,
            payload=payload,
        )

    assert service.get(
        workspace_id="workspace-1",
        agent_id="agent-1",
        facet="capabilities",
    ) is None


def test_self_model_uses_fixed_agent_memory_namespace(tmp_path: Path) -> None:
    store = _store(tmp_path)
    memory = MemoryService(store)
    service = SelfModelService(memory)
    service.compare_and_put(
        workspace_id="workspace-1",
        agent_id="agent-1",
        facet="capabilities",
        value={"v": 1},
        expected_revision_sha256=None,
    )

    records = memory.list_namespace(
        scope=MemoryScope.AGENT,
        owner_id="agent-1",
        namespace=SELF_MODEL_NAMESPACE,
    )

    assert len(records) == 1
    assert records[0].scope is MemoryScope.AGENT
    assert records[0].owner_id == "agent-1"
    assert records[0].namespace == SELF_MODEL_NAMESPACE


@pytest.mark.parametrize("facet", [" capabilities", "capabilities ", "capabilities/"])
def test_self_model_facet_identity_is_machine_safe(facet: str) -> None:
    with pytest.raises(ValueError, match="bounded machine token"):
        self_model_target_ref_sha256(
            workspace_id="workspace-1",
            agent_id="agent-1",
            facet=facet,
        )


def test_invalid_expected_revision_is_rejected_before_write(tmp_path: Path) -> None:
    service = SelfModelService(MemoryService(_store(tmp_path)))

    with pytest.raises(ValueError, match="SHA-256"):
        service.compare_and_put(
            workspace_id="workspace-1",
            agent_id="agent-1",
            facet="capabilities",
            value={"v": 1},
            expected_revision_sha256="not-a-digest",
        )

    assert service.get(
        workspace_id="workspace-1",
        agent_id="agent-1",
        facet="capabilities",
    ) is None
