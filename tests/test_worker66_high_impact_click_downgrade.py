"""QA_ONLY Worker66 oracle for V0.1 high-impact click risk downgrade.

DO NOT MERGE. The controlled adapter has no network, browser, account, message,
delete, or publish side effect. Canonical effect risk comes from backend ToolSpec;
the accessible control name is deliberately identical for every case.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from nika_core.interaction import (
    ApplicationIdentity,
    ControlLocator,
    ControlNode,
    InteractionAction,
    InteractionTarget,
    PermissionBlockedError,
    SemanticSnapshot,
)
from nika_core.interaction.orchestration import (
    InteractionRequest,
    InteractionRisk,
    SemanticInteractionCoordinator,
)
from nika_core.security.policy import (
    ApprovalLedger,
    ExecutionBudget,
    ExecutionBudgetLedger,
    SandboxPolicy,
    SecurityPolicy,
)
from nika_core.tools import ToolRisk, ToolSpec


@dataclass(frozen=True, slots=True)
class EffectCase:
    spec: ToolSpec

    @property
    def effect_id(self) -> str:
        return self.spec.tool_id.rsplit(".", 1)[-1]


_APPROVAL_EFFECTS = (
    EffectCase(
        ToolSpec(
            "interaction.effect.send",
            "Controlled send effect",
            risk=ToolRisk.EXTERNAL_SIDE_EFFECT,
        )
    ),
    EffectCase(
        ToolSpec(
            "interaction.effect.delete",
            "Controlled delete effect",
            risk=ToolRisk.HIGH_IMPACT,
        )
    ),
    EffectCase(
        ToolSpec(
            "interaction.effect.publish",
            "Controlled publish effect",
            risk=ToolRisk.HIGH_IMPACT,
        )
    ),
    EffectCase(
        ToolSpec(
            "interaction.effect.account_change",
            "Controlled account change effect",
            risk=ToolRisk.HIGH_IMPACT,
        )
    ),
)


class UnexpectedIdempotencyLedger:
    """R0 downgrade must not acquire side-effect identity as a substitute for authority."""

    def reserve_once(self, **kwargs: object) -> tuple[object, bool]:
        del kwargs
        raise AssertionError("downgraded R0 request unexpectedly reserved an external effect")

    def release_pending(self, operation_key: str) -> None:
        raise AssertionError(f"unexpected release for {operation_key}")

    def mark_uncertain(self, operation_key: str) -> None:
        raise AssertionError(f"unexpected uncertain effect for {operation_key}")

    def complete(self, operation_key: str, result: object) -> None:
        del result
        raise AssertionError(f"unexpected completion for {operation_key}")


class ControlledEffectAdapter:
    """In-memory stand-in whose effect truth is not derived from UI text."""

    def __init__(self, effect: EffectCase) -> None:
        self.effect = effect
        self.node = ControlNode(
            node_id=f"controlled-{effect.effect_id}",
            role="button",
            name="Continue",
        )
        self.snapshot = SemanticSnapshot(
            target=InteractionTarget(
                application=ApplicationIdentity("controlled-fixture.exe", 66, 1)
            ),
            generation=1,
            revision=1,
            controls=(self.node,),
        )
        self.focused: str | None = "before"
        self.attempted_effects: list[str] = []

    def observe(self) -> SemanticSnapshot:
        return self.snapshot

    def capture_focus(self) -> str | None:
        return self.focused

    def focus(self, node: ControlNode) -> None:
        self.focused = node.node_id

    def act(self, node: ControlNode, action: InteractionAction, value: str | None) -> None:
        assert node.node_id == self.node.node_id
        assert action is InteractionAction.INVOKE
        assert value is None
        self.attempted_effects.append(self.effect.effect_id)

    def verify(
        self,
        before: SemanticSnapshot,
        after: SemanticSnapshot,
        node: ControlNode,
        action: InteractionAction,
        value: str | None,
    ) -> bool:
        del before, after, node, action, value
        return True


def _coordinator(
    tmp_path: Path,
    effect: EffectCase,
) -> tuple[SemanticInteractionCoordinator, ControlledEffectAdapter]:
    adapter = ControlledEffectAdapter(effect)
    policy = SecurityPolicy(
        granted_tools=frozenset({effect.spec.tool_id}),
        sandbox=SandboxPolicy(workspace_root=tmp_path),
        budget=ExecutionBudget(),
    )
    coordinator = SemanticInteractionCoordinator(
        adapter=adapter,
        security_policy=policy,
        budgets=ExecutionBudgetLedger(policy.budget),
        approvals=ApprovalLedger(),
        idempotency=UnexpectedIdempotencyLedger(),  # type: ignore[arg-type]
    )
    return coordinator, adapter


def _generic_click(effect: EffectCase) -> InteractionRequest:
    return InteractionRequest(
        task_id="worker66-controlled-task",
        operation_key=f"worker66-{effect.effect_id}",
        tool_id=effect.spec.tool_id,
        target="controlled-resource-42",
        locator=ControlLocator(role="button", name="Continue"),
        action=InteractionAction.INVOKE,
        risk=InteractionRisk.R0_OBSERVE,
    )


@pytest.mark.parametrize("effect", _APPROVAL_EFFECTS, ids=lambda case: case.effect_id)
def test_generic_click_cannot_downgrade_canonical_effect_risk(
    tmp_path: Path,
    effect: EffectCase,
) -> None:
    """Backend ToolSpec requiring approval must fail closed before adapter.act."""
    assert effect.spec.risk in {ToolRisk.EXTERNAL_SIDE_EFFECT, ToolRisk.HIGH_IMPACT}
    coordinator, adapter = _coordinator(tmp_path, effect)

    with pytest.raises(PermissionBlockedError):
        coordinator.execute(_generic_click(effect))

    assert adapter.attempted_effects == []


def test_oracle_does_not_make_all_interactions_high_impact(tmp_path: Path) -> None:
    """A backend-declared harmless controlled local invoke remains a positive control."""
    harmless = EffectCase(
        ToolSpec(
            "interaction.effect.open_details",
            "Controlled local details effect",
            risk=ToolRisk.READ_ONLY,
        )
    )
    coordinator, adapter = _coordinator(tmp_path, harmless)

    result = coordinator.execute(_generic_click(harmless))

    assert result.succeeded is True
    assert adapter.attempted_effects == ["open_details"]
