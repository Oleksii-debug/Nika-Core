"""Fail-closed semantic interaction orchestration.

This layer owns the safety sequence around adapters. Framework-specific objects stay behind
``InteractionAdapter`` and never enter the Nika domain.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
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
from .resolver import resolve_strict, validate_action_target, validate_snapshot


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


def _detach_snapshot(snapshot: SemanticSnapshot) -> SemanticSnapshot:
    """Copy adapter-owned semantic evidence before any later adapter callback can mutate it."""
    target = snapshot.target
    application = None if target.application is None else replace(target.application)
    window = target.window
    if window is not None:
        window = replace(window, application=replace(window.application))
    browser = None if target.browser is None else replace(target.browser)
    detached_target = replace(
        target,
        application=application,
        window=window,
        browser=browser,
    )
    controls = tuple(
        replace(node, attributes=tuple((key, value) for key, value in node.attributes))
        for node in snapshot.controls
    )
    return replace(snapshot, target=detached_target, controls=controls)


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
    project_id: str | None = None

    def __post_init__(self) -> None:
        for value, label in (
            (self.task_id, "task_id"),
            (self.operation_key, "operation_key"),
            (self.tool_id, "tool_id"),
            (self.target, "target"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{label} must not be empty")
            if value != value.strip():
                raise ValueError(f"{label} must not contain surrounding whitespace")
        if self.project_id is not None:
            if not isinstance(self.project_id, str) or not self.project_id.strip():
                raise ValueError("project_id must not be empty when provided")
            if self.project_id != self.project_id.strip():
                raise ValueError("project_id must not contain surrounding whitespace")
        if not isinstance(self.locator, ControlLocator):
            raise TypeError("locator must be a ControlLocator")
        if not isinstance(self.action, InteractionAction):
            raise TypeError("action must be an InteractionAction")
        if not isinstance(self.risk, InteractionRisk):
            raise TypeError("risk must be an InteractionRisk")
        if self.value is not None and not isinstance(self.value, str):
            raise TypeError("interaction value must be a string when provided")

    @property
    def approval_intent(self) -> ActionIntent:
        """Canonical exact semantic effect used for approval and durable replay identity."""
        return ActionIntent(
            action_id=self.operation_key,
            tool_id=self.tool_id,
            risk=self.risk.tool_risk,
            target=self.target,
            approval_required=self.risk.approval_required,
            task_id=self.task_id,
            project_id=self.project_id,
            effect_id=self.operation_key,
            arguments={
                "action": self.action.value,
                "locator": {
                    "role": self.locator.role,
                    "name": self.locator.name,
                    "label": self.locator.label,
                    "text": self.locator.text,
                    "ancestor_node_id": self.locator.ancestor_node_id,
                    "attributes": [list(item) for item in self.locator.attributes],
                },
                "value": self.value,
            },
        )

    @property
    def fingerprint(self) -> str:
        return self.approval_intent.approval_fingerprint


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
        # Frozen dataclasses are still externally mutable via object.__setattr__.
        # Detach the request and nested locator before observing or authorizing.
        request = replace(
            request,
            locator=replace(
                request.locator,
                attributes=tuple((key, value) for key, value in request.locator.attributes),
            ),
        )
        observed = _detach_snapshot(self.adapter.observe())
        node = resolve_strict(observed, request.locator)

        current = _detach_snapshot(self.adapter.observe())
        validate_snapshot(observed, current)
        current_node = resolve_strict(current, request.locator)
        validate_action_target(node, current_node)

        # Freeze one canonical effect before durable state or trusted policy calls.
        intent = request.approval_intent
        reserved = False
        if request.risk.durable_side_effect:
            record, created = self.idempotency.reserve_once(
                operation_key=request.operation_key,
                task_id=request.task_id,
                operation_type="interaction.execute",
                input_fingerprint=intent.approval_fingerprint,
            )
            if not created:
                if record.status is IdempotencyStatus.COMPLETED:
                    raise InteractionReplayBlockedError("interaction side effect already completed")
                raise InteractionReplayBlockedError(
                    "pending or uncertain interaction requires reconciliation before retry"
                )
            reserved = True

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
            pre_focus_node = current_node
            self.adapter.focus(current_node)
            focused = self.adapter.capture_focus()
            if focused != current_node.node_id:
                raise StaleSnapshotError("Semantic target did not receive verified focus")

            # A focus handler can change semantics or navigate. Snapshot the new adapter
            # evidence before any later callback can mutate a reused carrier.
            pre_action = _detach_snapshot(self.adapter.observe())
            if pre_action.target != current.target or pre_action.generation != current.generation:
                raise StaleSnapshotError("Interaction target changed after focus")
            action_node = resolve_strict(pre_action, request.locator)
            validate_action_target(pre_focus_node, action_node)

            action_started = True
            self.adapter.act(action_node, request.action, request.value)
            after = _detach_snapshot(self.adapter.observe())
            if not self.adapter.verify(
                pre_action, after, action_node, request.action, request.value
            ):
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
                    snapshot_generation=pre_action.generation,
                    snapshot_revision=pre_action.revision,
                    matched_node_id=action_node.node_id,
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
