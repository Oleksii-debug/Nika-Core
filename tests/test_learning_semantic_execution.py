from __future__ import annotations

from pathlib import Path

import pytest

from nika_core.data.sqlite import SQLiteStore
from nika_core.learned_skill import (
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
from nika_core.learning_memory import (
    MEMORY_UPDATE_SCHEMA,
    LearningMemoryApplier,
    memory_target_ref_sha256,
)
from nika_core.learning_self_model import (
    SELF_MODEL_UPDATE_SCHEMA,
    LearningSelfModelApplier,
)
from nika_core.learning_semantic_execution import (
    LearningSemanticReconciliationRequired,
    LearningSemanticUpdateExecutor,
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
from nika_core.memory.contracts import MemoryConflictError, MemoryScope
from nika_core.memory.service import MemoryService
from nika_core.runtime.idempotency import (
    IdempotencyConflictError,
    IdempotencyLedger,
    IdempotencyStatus,
)
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
        candidate_id="candidate-semantic-execution-1",
        workspace_id="workspace-1",
        agent_id="agent-1",
        kind=CognitionCandidateKind.HYPOTHESIS,
        statement="verified durable semantic execution",
        evidence=(
            CognitionEvidenceRef(
                source_type="comparison",
                source_id="comparison-semantic-execution-1",
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


def _runtime(
    tmp_path: Path,
) -> tuple[SQLiteStore, MemoryService, LearningSemanticUpdateExecutor]:
    store = SQLiteStore(tmp_path / "semantic replay" / "nika.db")
    store.initialize()
    memory = MemoryService(store)
    router = LearningSemanticUpdateRouter(
        memory=LearningMemoryApplier(memory),
        world_model=LearningWorldModelApplier(WorldModelService(memory)),
        self_model=LearningSelfModelApplier(SelfModelService(memory)),
        skill=LearningSkillApplier(LearnedSkillService(memory)),
    )
    return (
        store,
        memory,
        LearningSemanticUpdateExecutor(
            router=router,
            idempotency=IdempotencyLedger(store),
        ),
    )


def _intent(
    *,
    candidate: CognitionCandidate,
    verification: CognitionVerification,
    intent_id: str,
    target: LearningUpdateTarget,
    target_ref_sha256: str,
    update_schema: str,
    payload: bytes,
    expected_revision_sha256: str | None = None,
) -> LearningUpdateIntent:
    return LearningUpdateIntent.bind_payload(
        intent_id=intent_id,
        candidate=candidate,
        verification=verification,
        expected_verification_policy_sha256=POLICY,
        expected_requirements=_requirements(),
        target=target,
        target_ref_sha256=target_ref_sha256,
        update_schema=update_schema,
        payload=payload,
        expected_revision_sha256=expected_revision_sha256,
    )


def _apply(
    executor: LearningSemanticUpdateExecutor,
    *,
    task_id: str,
    intent: LearningUpdateIntent,
    candidate: CognitionCandidate,
    verification: CognitionVerification,
    payload: bytes,
    address: object,
):
    return executor.apply(
        task_id=task_id,
        intent=intent,
        candidate=candidate,
        verification=verification,
        expected_verification_policy_sha256=POLICY,
        expected_requirements=_requirements(),
        payload=payload,
        address=address,  # type: ignore[arg-type]
    )


def _semantic_cases(
    candidate: CognitionCandidate,
    verification: CognitionVerification,
):
    memory_payload = b'{"kind":"memory"}'
    world_payload = b'{"kind":"world"}'
    self_payload = b'{"kind":"self"}'
    skill_payload = b'{"kind":"skill"}'
    return (
        (
            "task-memory",
            _intent(
                candidate=candidate,
                verification=verification,
                intent_id="semantic-execution-memory",
                target=LearningUpdateTarget.MEMORY,
                target_ref_sha256=memory_target_ref_sha256(
                    scope=MemoryScope.AGENT,
                    owner_id=candidate.agent_id,
                    namespace="learning.replay",
                    key="memory-1",
                ),
                update_schema=MEMORY_UPDATE_SCHEMA,
                payload=memory_payload,
            ),
            memory_payload,
            MemoryUpdateAddress(
                scope=MemoryScope.AGENT,
                owner_id=candidate.agent_id,
                namespace="learning.replay",
                key="memory-1",
            ),
        ),
        (
            "task-world",
            _intent(
                candidate=candidate,
                verification=verification,
                intent_id="semantic-execution-world",
                target=LearningUpdateTarget.WORLD_MODEL,
                target_ref_sha256=world_model_target_ref_sha256(
                    workspace_id=candidate.workspace_id,
                    topic="environment",
                ),
                update_schema=WORLD_MODEL_UPDATE_SCHEMA,
                payload=world_payload,
            ),
            world_payload,
            WorldModelUpdateAddress(
                workspace_id=candidate.workspace_id,
                topic="environment",
            ),
        ),
        (
            "task-self",
            _intent(
                candidate=candidate,
                verification=verification,
                intent_id="semantic-execution-self",
                target=LearningUpdateTarget.SELF_MODEL,
                target_ref_sha256=self_model_target_ref_sha256(
                    workspace_id=candidate.workspace_id,
                    agent_id=candidate.agent_id,
                    facet="capabilities",
                ),
                update_schema=SELF_MODEL_UPDATE_SCHEMA,
                payload=self_payload,
            ),
            self_payload,
            SelfModelUpdateAddress(
                workspace_id=candidate.workspace_id,
                agent_id=candidate.agent_id,
                facet="capabilities",
            ),
        ),
        (
            "task-skill",
            _intent(
                candidate=candidate,
                verification=verification,
                intent_id="semantic-execution-skill",
                target=LearningUpdateTarget.SKILL,
                target_ref_sha256=learned_skill_target_ref_sha256(
                    workspace_id=candidate.workspace_id,
                    agent_id=candidate.agent_id,
                    skill_id="document-triage",
                ),
                update_schema=SKILL_UPDATE_SCHEMA,
                payload=skill_payload,
            ),
            skill_payload,
            SkillUpdateAddress(
                workspace_id=candidate.workspace_id,
                agent_id=candidate.agent_id,
                skill_id="document-triage",
            ),
        ),
    )


def test_completed_execution_replays_all_targets_without_second_effect(
    tmp_path: Path,
) -> None:
    store, _memory, executor = _runtime(tmp_path)
    candidate = _candidate()
    verification = _verification(candidate)

    for task_id, intent, payload, address in _semantic_cases(candidate, verification):
        first = _apply(
            executor,
            task_id=task_id,
            intent=intent,
            candidate=candidate,
            verification=verification,
            payload=payload,
            address=address,
        )
        replay = _apply(
            executor,
            task_id=task_id,
            intent=intent,
            candidate=candidate,
            verification=verification,
            payload=payload,
            address=address,
        )

        assert first.replayed is False
        assert replay.replayed is True
        assert replay.target is intent.target
        assert replay.intent_sha256 == first.intent_sha256
        assert replay.target_ref_sha256 == first.target_ref_sha256
        assert replay.revision_sha256 == first.revision_sha256
        assert replay.durable_value_sha256 == first.durable_value_sha256
        assert replay.created is first.created

    records = []
    ledger = IdempotencyLedger(store)
    for task_id, _intent_value, _payload, _address in _semantic_cases(
        candidate,
        verification,
    ):
        records.extend(ledger.list_for_task(task_id))
    assert len(records) == 4
    assert {record.status for record in records} == {IdempotencyStatus.COMPLETED}


def test_completed_replay_still_revalidates_bound_address(tmp_path: Path) -> None:
    _store, memory, executor = _runtime(tmp_path)
    candidate = _candidate()
    verification = _verification(candidate)
    payload = b'{"kind":"memory"}'
    intent = _intent(
        candidate=candidate,
        verification=verification,
        intent_id="semantic-execution-address",
        target=LearningUpdateTarget.MEMORY,
        target_ref_sha256=memory_target_ref_sha256(
            scope=MemoryScope.AGENT,
            owner_id=candidate.agent_id,
            namespace="learning.replay",
            key="bound-key",
        ),
        update_schema=MEMORY_UPDATE_SCHEMA,
        payload=payload,
    )
    good = MemoryUpdateAddress(
        scope=MemoryScope.AGENT,
        owner_id=candidate.agent_id,
        namespace="learning.replay",
        key="bound-key",
    )
    _apply(
        executor,
        task_id="task-address",
        intent=intent,
        candidate=candidate,
        verification=verification,
        payload=payload,
        address=good,
    )

    with pytest.raises(ValueError, match="address does not match"):
        _apply(
            executor,
            task_id="task-address",
            intent=intent,
            candidate=candidate,
            verification=verification,
            payload=payload,
            address=MemoryUpdateAddress(
                scope=MemoryScope.AGENT,
                owner_id=candidate.agent_id,
                namespace="learning.replay",
                key="other-key",
            ),
        )

    assert memory.get(
        scope=MemoryScope.AGENT,
        owner_id=candidate.agent_id,
        namespace="learning.replay",
        key="other-key",
    ) is None


@pytest.mark.parametrize("status", [IdempotencyStatus.PENDING, IdempotencyStatus.UNCERTAIN])
def test_unresolved_prior_execution_blocks_replay_before_target_effect(
    tmp_path: Path,
    status: IdempotencyStatus,
) -> None:
    store, memory, executor = _runtime(tmp_path)
    candidate = _candidate()
    verification = _verification(candidate)
    payload = b'{"kind":"memory"}'
    intent = _intent(
        candidate=candidate,
        verification=verification,
        intent_id=f"semantic-execution-{status.value}",
        target=LearningUpdateTarget.MEMORY,
        target_ref_sha256=memory_target_ref_sha256(
            scope=MemoryScope.AGENT,
            owner_id=candidate.agent_id,
            namespace="learning.replay",
            key=status.value,
        ),
        update_schema=MEMORY_UPDATE_SCHEMA,
        payload=payload,
    )
    address = MemoryUpdateAddress(
        scope=MemoryScope.AGENT,
        owner_id=candidate.agent_id,
        namespace="learning.replay",
        key=status.value,
    )
    _apply(
        executor,
        task_id=f"task-{status.value}",
        intent=intent,
        candidate=candidate,
        verification=verification,
        payload=payload,
        address=address,
    )
    ledger = IdempotencyLedger(store)
    record = ledger.list_for_task(f"task-{status.value}")[0]
    with store.connection() as conn:
        conn.execute(
            "UPDATE idempotency_records SET status = ?, result_json = NULL "
            "WHERE operation_key = ?",
            (status.value, record.operation_key),
        )

    before = memory.get(
        scope=MemoryScope.AGENT,
        owner_id=candidate.agent_id,
        namespace="learning.replay",
        key=status.value,
    )
    assert before is not None

    with pytest.raises(
        LearningSemanticReconciliationRequired,
        match="unresolved prior execution",
    ):
        _apply(
            executor,
            task_id=f"task-{status.value}",
            intent=intent,
            candidate=candidate,
            verification=verification,
            payload=payload,
            address=address,
        )

    after = memory.get(
        scope=MemoryScope.AGENT,
        owner_id=candidate.agent_id,
        namespace="learning.replay",
        key=status.value,
    )
    assert after == before


def test_router_failure_marks_owned_reservation_uncertain(tmp_path: Path) -> None:
    store, memory, executor = _runtime(tmp_path)
    candidate = _candidate()
    verification = _verification(candidate)
    memory.put(
        scope=MemoryScope.AGENT,
        owner_id=candidate.agent_id,
        namespace="learning.replay",
        key="stale-update",
        value={"kind": "existing"},
    )
    payload = b'{"kind":"replacement"}'
    intent = _intent(
        candidate=candidate,
        verification=verification,
        intent_id="semantic-execution-stale",
        target=LearningUpdateTarget.MEMORY,
        target_ref_sha256=memory_target_ref_sha256(
            scope=MemoryScope.AGENT,
            owner_id=candidate.agent_id,
            namespace="learning.replay",
            key="stale-update",
        ),
        update_schema=MEMORY_UPDATE_SCHEMA,
        payload=payload,
        expected_revision_sha256="d" * 64,
    )

    with pytest.raises(MemoryConflictError, match="revision changed"):
        _apply(
            executor,
            task_id="task-stale",
            intent=intent,
            candidate=candidate,
            verification=verification,
            payload=payload,
            address=MemoryUpdateAddress(
                scope=MemoryScope.AGENT,
                owner_id=candidate.agent_id,
                namespace="learning.replay",
                key="stale-update",
            ),
        )

    records = IdempotencyLedger(store).list_for_task("task-stale")
    assert len(records) == 1
    assert records[0].status is IdempotencyStatus.UNCERTAIN
    current = memory.get(
        scope=MemoryScope.AGENT,
        owner_id=candidate.agent_id,
        namespace="learning.replay",
        key="stale-update",
    )
    assert current is not None
    assert current.value == {"kind": "existing"}


def test_completed_intent_cannot_be_rebound_to_another_task(tmp_path: Path) -> None:
    _store, _memory, executor = _runtime(tmp_path)
    candidate = _candidate()
    verification = _verification(candidate)
    task_id, intent, payload, address = _semantic_cases(candidate, verification)[0]
    first = _apply(
        executor,
        task_id=task_id,
        intent=intent,
        candidate=candidate,
        verification=verification,
        payload=payload,
        address=address,
    )
    assert first.replayed is False

    with pytest.raises(IdempotencyConflictError, match="different operation input"):
        _apply(
            executor,
            task_id="another-task",
            intent=intent,
            candidate=candidate,
            verification=verification,
            payload=payload,
            address=address,
        )


def test_durable_execution_receipt_does_not_expose_semantic_payload(
    tmp_path: Path,
) -> None:
    store, _memory, executor = _runtime(tmp_path)
    candidate = _candidate()
    verification = _verification(candidate)
    secret = "private-semantic-value"
    payload = ('{"note":"' + secret + '"}').encode()
    intent = _intent(
        candidate=candidate,
        verification=verification,
        intent_id="semantic-execution-private",
        target=LearningUpdateTarget.SKILL,
        target_ref_sha256=learned_skill_target_ref_sha256(
            workspace_id=candidate.workspace_id,
            agent_id=candidate.agent_id,
            skill_id="privacy",
        ),
        update_schema=SKILL_UPDATE_SCHEMA,
        payload=payload,
    )
    receipt = _apply(
        executor,
        task_id="task-private",
        intent=intent,
        candidate=candidate,
        verification=verification,
        payload=payload,
        address=SkillUpdateAddress(
            workspace_id=candidate.workspace_id,
            agent_id=candidate.agent_id,
            skill_id="privacy",
        ),
    )

    record = IdempotencyLedger(store).list_for_task("task-private")[0]
    assert secret not in repr(receipt)
    assert secret not in repr(record.result)
