from __future__ import annotations

import hmac
import re
from dataclasses import dataclass

from nika_core.learned_skill import learned_skill_target_ref_sha256
from nika_core.learning_cognition import (
    CognitionCandidate,
    CognitionVerification,
    CognitionVerificationRequirement,
)
from nika_core.learning_memory import (
    LearningMemoryApplyReceipt,
    memory_target_ref_sha256,
)
from nika_core.learning_self_model import LearningSelfModelApplyReceipt
from nika_core.learning_semantic_update import (
    LearningSemanticUpdateRouter,
    MemoryUpdateAddress,
    SelfModelUpdateAddress,
    SemanticUpdateAddress,
    SkillUpdateAddress,
    WorldModelUpdateAddress,
)
from nika_core.learning_skill import LearningSkillApplyReceipt
from nika_core.learning_update import LearningUpdateIntent, LearningUpdateTarget
from nika_core.learning_world_model import LearningWorldModelApplyReceipt
from nika_core.runtime.idempotency import (
    IdempotencyLedger,
    IdempotencyRecord,
    IdempotencyStatus,
)
from nika_core.self_model import self_model_target_ref_sha256
from nika_core.world_model import world_model_target_ref_sha256

_OPERATION_TYPE = "learning.semantic_update"
_OPERATION_PREFIX = "learning-semantic-update:"
_RESULT_SCHEMA = "nika.learning-semantic-execution-receipt/v1"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\\Z")


class LearningSemanticReconciliationRequired(RuntimeError):
    """A prior semantic effect has an ambiguous durable execution outcome."""


@dataclass(frozen=True, slots=True)
class LearningSemanticExecutionReceipt:
    target: LearningUpdateTarget
    intent_sha256: str
    target_ref_sha256: str
    revision_sha256: str
    durable_value_sha256: str
    created: bool
    replayed: bool

    def __post_init__(self) -> None:
        if type(self.target) is not LearningUpdateTarget:
            raise TypeError("target must be LearningUpdateTarget")
        for name in (
            "intent_sha256",
            "target_ref_sha256",
            "revision_sha256",
            "durable_value_sha256",
        ):
            value = getattr(self, name)
            if type(value) is not str or not _SHA256_RE.fullmatch(value):
                raise ValueError(f"{name} must be lowercase SHA-256")
        if type(self.created) is not bool:
            raise TypeError("created must be exact bool")
        if type(self.replayed) is not bool:
            raise TypeError("replayed must be exact bool")


TargetApplyReceipt = (
    LearningMemoryApplyReceipt
    | LearningWorldModelApplyReceipt
    | LearningSelfModelApplyReceipt
    | LearningSkillApplyReceipt
)


def _operation_key(intent: LearningUpdateIntent) -> str:
    return _OPERATION_PREFIX + intent.intent_sha256


def _expected_receipt_type(target: LearningUpdateTarget) -> type[object]:
    if target is LearningUpdateTarget.MEMORY:
        return LearningMemoryApplyReceipt
    if target is LearningUpdateTarget.WORLD_MODEL:
        return LearningWorldModelApplyReceipt
    if target is LearningUpdateTarget.SELF_MODEL:
        return LearningSelfModelApplyReceipt
    if target is LearningUpdateTarget.SKILL:
        return LearningSkillApplyReceipt
    raise ValueError("unsupported learning update target")


def _canonical_address_target_ref(
    *,
    target: LearningUpdateTarget,
    address: SemanticUpdateAddress,
) -> str:
    if target is LearningUpdateTarget.MEMORY:
        if type(address) is not MemoryUpdateAddress:
            raise TypeError("MEMORY intent requires MemoryUpdateAddress")
        target_ref = memory_target_ref_sha256(
            scope=address.scope,
            owner_id=address.owner_id,
            namespace=address.namespace,
            key=address.key,
        )
    elif target is LearningUpdateTarget.WORLD_MODEL:
        if type(address) is not WorldModelUpdateAddress:
            raise TypeError("WORLD_MODEL intent requires WorldModelUpdateAddress")
        target_ref = world_model_target_ref_sha256(
            workspace_id=address.workspace_id,
            topic=address.topic,
        )
    elif target is LearningUpdateTarget.SELF_MODEL:
        if type(address) is not SelfModelUpdateAddress:
            raise TypeError("SELF_MODEL intent requires SelfModelUpdateAddress")
        target_ref = self_model_target_ref_sha256(
            workspace_id=address.workspace_id,
            agent_id=address.agent_id,
            facet=address.facet,
        )
    elif target is LearningUpdateTarget.SKILL:
        if type(address) is not SkillUpdateAddress:
            raise TypeError("SKILL intent requires SkillUpdateAddress")
        target_ref = learned_skill_target_ref_sha256(
            workspace_id=address.workspace_id,
            agent_id=address.agent_id,
            skill_id=address.skill_id,
        )
    else:
        raise ValueError("unsupported learning update target")
    return target_ref


def _require_sha256(value: object, *, field: str) -> str:
    if type(value) is not str or not _SHA256_RE.fullmatch(value):
        raise RuntimeError(f"durable semantic receipt {field} is invalid")
    return value


def _snapshot_target_receipt(
    *,
    receipt: TargetApplyReceipt,
    intent: LearningUpdateIntent,
) -> LearningSemanticExecutionReceipt:
    expected_type = _expected_receipt_type(intent.target)
    if type(receipt) is not expected_type:
        raise RuntimeError("semantic router returned a receipt for the wrong target")
    try:
        intent_sha256 = _require_sha256(receipt.intent_sha256, field="intent_sha256")
        target_ref_sha256 = _require_sha256(
            receipt.target_ref_sha256,
            field="target_ref_sha256",
        )
        revision_sha256 = _require_sha256(
            receipt.revision_sha256,
            field="revision_sha256",
        )
        durable_value_sha256 = _require_sha256(
            receipt.durable_value_sha256,
            field="durable_value_sha256",
        )
        created = receipt.created
    except AttributeError as exc:
        raise RuntimeError("semantic router receipt is missing canonical fields") from exc
    if type(created) is not bool:
        raise RuntimeError("semantic router receipt created flag is invalid")
    if not hmac.compare_digest(intent_sha256, intent.intent_sha256):
        raise RuntimeError("semantic router receipt does not match the canonical intent")
    if not hmac.compare_digest(target_ref_sha256, intent.target_ref_sha256):
        raise RuntimeError("semantic router receipt does not match the canonical target")
    return LearningSemanticExecutionReceipt(
        target=intent.target,
        intent_sha256=intent_sha256,
        target_ref_sha256=target_ref_sha256,
        revision_sha256=revision_sha256,
        durable_value_sha256=durable_value_sha256,
        created=created,
        replayed=False,
    )


def _result_payload(receipt: LearningSemanticExecutionReceipt) -> dict[str, object]:
    return {
        "created": receipt.created,
        "durable_value_sha256": receipt.durable_value_sha256,
        "intent_sha256": receipt.intent_sha256,
        "revision_sha256": receipt.revision_sha256,
        "schema": _RESULT_SCHEMA,
        "target": receipt.target.value,
        "target_ref_sha256": receipt.target_ref_sha256,
    }


def _replayed_receipt(
    *,
    record: IdempotencyRecord,
    intent: LearningUpdateIntent,
) -> LearningSemanticExecutionReceipt:
    result = record.result
    if type(result) is not dict:
        raise RuntimeError("completed semantic execution lacks a canonical durable result")
    expected_keys = {
        "created",
        "durable_value_sha256",
        "intent_sha256",
        "revision_sha256",
        "schema",
        "target",
        "target_ref_sha256",
    }
    if set(result) != expected_keys:
        raise RuntimeError("completed semantic execution result has the wrong schema")
    if result["schema"] != _RESULT_SCHEMA:
        raise RuntimeError("completed semantic execution result schema is unsupported")
    if result["target"] != intent.target.value:
        raise RuntimeError("completed semantic execution result target does not match intent")

    intent_sha256 = _require_sha256(result["intent_sha256"], field="intent_sha256")
    target_ref_sha256 = _require_sha256(
        result["target_ref_sha256"],
        field="target_ref_sha256",
    )
    revision_sha256 = _require_sha256(
        result["revision_sha256"],
        field="revision_sha256",
    )
    durable_value_sha256 = _require_sha256(
        result["durable_value_sha256"],
        field="durable_value_sha256",
    )
    created = result["created"]
    if type(created) is not bool:
        raise RuntimeError("completed semantic execution created flag is invalid")
    if not hmac.compare_digest(intent_sha256, intent.intent_sha256):
        raise RuntimeError("completed semantic execution intent identity changed")
    if not hmac.compare_digest(target_ref_sha256, intent.target_ref_sha256):
        raise RuntimeError("completed semantic execution target identity changed")
    return LearningSemanticExecutionReceipt(
        target=intent.target,
        intent_sha256=intent_sha256,
        target_ref_sha256=target_ref_sha256,
        revision_sha256=revision_sha256,
        durable_value_sha256=durable_value_sha256,
        created=created,
        replayed=True,
    )


class LearningSemanticUpdateExecutor:
    """Atomic replay fence over the canonical Loop-B semantic router."""

    def __init__(
        self,
        *,
        router: LearningSemanticUpdateRouter,
        idempotency: IdempotencyLedger,
    ) -> None:
        if type(router) is not LearningSemanticUpdateRouter:
            raise TypeError("router must be the canonical LearningSemanticUpdateRouter")
        if type(idempotency) is not IdempotencyLedger:
            raise TypeError("idempotency must be the canonical IdempotencyLedger")
        if router.sqlite_store is not idempotency.sqlite_store:
            raise ValueError("semantic router and idempotency must share one SQLiteStore")
        self._router = router
        self._idempotency = idempotency
        self._store = router.sqlite_store

    def apply(
        self,
        *,
        task_id: str,
        intent: LearningUpdateIntent,
        candidate: CognitionCandidate,
        verification: CognitionVerification,
        expected_verification_policy_sha256: str,
        expected_requirements: tuple[CognitionVerificationRequirement, ...],
        payload: bytes,
        address: SemanticUpdateAddress,
    ) -> LearningSemanticExecutionReceipt:
        canonical = LearningUpdateIntent.revalidate(
            intent,
            candidate=candidate,
            verification=verification,
            expected_verification_policy_sha256=expected_verification_policy_sha256,
            expected_requirements=expected_requirements,
            payload=payload,
        )
        target_ref = _canonical_address_target_ref(
            target=canonical.target,
            address=address,
        )
        if not hmac.compare_digest(target_ref, canonical.target_ref_sha256):
            raise ValueError("semantic update address does not match the bound target")

        operation_key = _operation_key(canonical)
        with self._store.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            record, created_reservation = self._idempotency.reserve_with_connection(
                conn,
                operation_key=operation_key,
                task_id=task_id,
                operation_type=_OPERATION_TYPE,
                input_fingerprint=canonical.intent_sha256,
            )
            if not created_reservation:
                if record.status is IdempotencyStatus.COMPLETED:
                    return _replayed_receipt(record=record, intent=canonical)
                if record.status in {
                    IdempotencyStatus.PENDING,
                    IdempotencyStatus.UNCERTAIN,
                }:
                    raise LearningSemanticReconciliationRequired(
                        "semantic update has an unresolved prior execution"
                    )
                raise RuntimeError("semantic update has unsupported idempotency state")

            target_receipt = self._router.apply_with_connection(
                conn,
                intent=canonical,
                candidate=candidate,
                verification=verification,
                expected_verification_policy_sha256=expected_verification_policy_sha256,
                expected_requirements=expected_requirements,
                payload=payload,
                address=address,
            )
            receipt = _snapshot_target_receipt(
                receipt=target_receipt,
                intent=canonical,
            )
            self._idempotency.complete_pending_if_matches_with_connection(
                conn,
                operation_key=record.operation_key,
                task_id=record.task_id,
                operation_type=record.operation_type,
                input_fingerprint=record.input_fingerprint,
                created_at=record.created_at,
                result=_result_payload(receipt),
            )
            return receipt
