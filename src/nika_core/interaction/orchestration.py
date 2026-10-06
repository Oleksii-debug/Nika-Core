"""Fail-closed semantic interaction orchestration.

This layer owns the safety sequence around adapters. Framework-specific objects stay behind
``InteractionAdapter`` and never enter the Nika domain.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from nika_core.runtime.idempotency import IdempotencyLedger, IdempotencyStatus
from nika_core.security.policy import (
    ActionIntent,
    ApprovalEvidence,
    ApprovalLedger,
    ExecutionBudgetLedger,
    SecurityPolicy,
    authorize_action,
)
from nika_core.tools import ToolRisk

from .domain import (
    ControlLocator,
    ControlNode,
    InteractionAction,
    InteractionEvidence,
    InteractionResult,
    PermissionBlockedError,
    SemanticSnapshot,
    StaleSnapshotError,
)
from .resolver import resolve_strict, validate_snapshot


class InteractionRisk(StrEnum):
    R0_OBSERVE = "r0_observe"
    R1_LOCAL_REVERSIBLE = "r1_local_reversible"
    R2_EXTERNAL_SIDE_EFFECT = "r2_external_side_effect"
    R3_SENSITIVE = "r3_sensitive"
    R4_HIGH_IMPACT = "r4_high_impact"

    @property
    def tool_risk(self) -> ToolRisk:
        if self is InteractionRisk.R0_OBSERVE:
            return ToolRisk.READ_ONLY
        if self is InteractionRisk.R1_LOCAL_REVERSIBLE:
            return ToolRisk.LOCAL_WRITE
        if self in {InteractionRisk.R2_EXTERNAL_SIDE_EFFECT, InteractionRisk.R3_SENSITIVE}:
            return ToolRisk.EXTERNAL_SIDE_EFFECT
        return ToolRisk.HIGH_IMPACT

    @property
    def approval_required(self) -> bool:
        return self in {
            InteractionRisk.R2_EXTERNAL_SIDE_EFFECT,
            InteractionRisk.R3_SENSITIVE,
            InteractionRisk.R4_HIGH_IMPACT,
        }

    @property
    def durable_side_effect(self) -> bool:
        return self in {
            InteractionRisk.R2_EXTERNAL_SIDE_EFFECT,
            InteractionRisk.R3_SENSITIVE,
            InteractionRisk.R4_HIGH_IMPACT,
        }


class InteractionAdapter(Protocol):
    """Semantic adapter boundary implemented by Playwright/UIA backends."""

    def observe(self) -> SemanticSnapshot: ...

    def capture_focus(self) -> str | None: ...

    def focus(self, node: ControlNode) -> None: ...

    def act(self, node: ControlNode, action: InteractionAction, value: str | None) -> None: ...

    def verify(
        self,
        before: SemanticSnapshot,
        after: SemanticSnapshot,
        node: ControlNode,
        action: InteractionAction,
        value: str | None,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class InteractionRequest:
    task_id: str
    operation_key: str
    tool_id: str
    target: str
    locator: ControlLocator
    action: InteractionAction
    risk: InteractionRisk
    value: str | None = None

    def __post_init__(self) -> None:
        if not self.task_id.strip() or not self.operation_key.strip():
            raise ValueError("task_id and operation_key must not be empty")
        if not self.tool_id.strip() or not self.target.strip():
            raise ValueError("tool_id and target must not be empty")

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(
            [
                "nika-interaction-v1",
                self.tool_id,
                self.target,
                self.action.value,
                self.risk.value,
                self.locator.role,
                self.locator.name,
                self.locator.label,
                self.locator.text,
                self.locator.ancestor_node_id,
                list(self.locator.attributes),
                self.value,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class InteractionUncertainError(RuntimeError):
    """External state may have changed; blind retry is forbidden until reconciliation."""


class InteractionReplayBlockedError(RuntimeError):
    """A prior pending/uncertain side effect blocks replay."""


@dataclass(slots=True)
class SemanticInteractionCoordinator:
    adapter: InteractionAdapter
    security_policy: SecurityPolicy
    budgets: ExecutionBudgetLedger
    approvals: ApprovalLedger
    idempotency: IdempotencyLedger

    def execute(
        self,
        request: InteractionRequest,
        *,
        approval: ApprovalEvidence | None = None,
    ) -> InteractionResult:
        """OBSERVE -> RESOLVE -> VALIDATE -> AUTHORIZE -> ACT -> VERIFY.

        External side effects are durably reserved before authorization. Permission denial is a
        terminal block and releases a reservation because no adapter action was attempted.
        Adapter failure after an external action starts becomes UNCERTAIN and is never retried
        blindly.
        """
        observed = self.adapter.observe()
        node = resolve_strict(observed, request.locator)

        current = self.adapter.observe()
        validate_snapshot(observed, current)
        current_node = resolve_strict(current, request.locator)
        if current_node.node_id != node.node_id:
            raise StaleSnapshotError("Resolved semantic node changed during validation")

        reserved = False
        if request.risk.durable_side_effect:
            record, created = self.idempotency.reserve_once(
                operation_key=request.operation_key,
                task_id=request.task_id,
                operation_type="interaction.execute",
                input_fingerprint=request.fingerprint,
            )
            if not created:
                if record.status is IdempotencyStatus.COMPLETED:
                    raise InteractionReplayBlockedError("interaction side effect already completed")
                raise InteractionReplayBlockedError(
                    "pending or uncertain interaction requires reconciliation before retry"
                )
            reserved = True

        intent = ActionIntent(
            action_id=request.operation_key,
            tool_id=request.tool_id,
            risk=request.risk.tool_risk,
            target=request.target,
            approval_required=request.risk.approval_required,
        )
        try:
            authorize_action(
                intent,
                self.security_policy,
                self.budgets,
                self.approvals,
                approval=approval,
            )
        except PermissionError as exc:
            if reserved:
                self.idempotency.release_pending(request.operation_key)
            raise PermissionBlockedError(str(exc)) from exc

        focus_before = self.adapter.capture_focus()
        action_started = False
        try:
            self.adapter.focus(current_node)
            focused = self.adapter.capture_focus()
            if focused != current_node.node_id:
                raise StaleSnapshotError("Semantic target did not receive verified focus")

            action_started = True
            self.adapter.act(current_node, request.action, request.value)
            after = self.adapter.observe()
            if not self.adapter.verify(current, after, current_node, request.action, request.value):
                if reserved:
                    self.idempotency.mark_uncertain(request.operation_key)
                raise InteractionUncertainError(
                    "Postcondition was not proven; reconcile external state before retry"
                )

            if reserved:
                self.idempotency.complete(
                    request.operation_key,
                    {
                        "action": request.action.value,
                        "target": request.target,
                        "node_id": current_node.node_id,
                    },
                )
            focus_after = self.adapter.capture_focus()
            return InteractionResult(
                succeeded=True,
                action=request.action,
                evidence=InteractionEvidence(
                    snapshot_generation=current.generation,
                    snapshot_revision=current.revision,
                    matched_node_id=current_node.node_id,
                    focus_before=focus_before,
                    focus_after=focus_after,
                    details=(("risk", request.risk.value),),
                ),
                message="semantic interaction verified",
            )
        except InteractionUncertainError:
            raise
        except Exception as exc:
            if reserved:
                if action_started:
                    self.idempotency.mark_uncertain(request.operation_key)
                    raise InteractionUncertainError(
                        "Adapter failed after action start; reconcile before retry"
                    ) from exc
                self.idempotency.release_pending(request.operation_key)
            raise
