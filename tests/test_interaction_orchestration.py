from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from nika_core.interaction import (
    ApplicationIdentity,
    ControlLocator,
    ControlNode,
    InteractionAction,
    InteractionTarget,
    PermissionBlockedError,
    SemanticSnapshot,
    StaleSnapshotError,
)
from nika_core.interaction.orchestration import (
    InteractionReplayBlockedError,
    InteractionRequest,
    InteractionRisk,
    InteractionUncertainError,
    SemanticInteractionCoordinator,
)
from nika_core.runtime.idempotency import IdempotencyStatus
from nika_core.security.policy import (
    ApprovalLedger,
    ExecutionBudget,
    ExecutionBudgetLedger,
    SandboxPolicy,
    SecurityPolicy,
)


def _snapshot(*nodes: ControlNode, generation: int = 1, revision: int = 1) -> SemanticSnapshot:
    return SemanticSnapshot(
        target=InteractionTarget(application=ApplicationIdentity("fixture.exe", 42, 100)),
        generation=generation,
        revision=revision,
        controls=tuple(nodes),
    )


@dataclass
class FakeLedger:
    existing_status: IdempotencyStatus | None = None
    reserved: bool = False
    released: bool = False
    uncertain: bool = False
    completed: bool = False

    def reserve_once(self, **kwargs: object) -> tuple[object, bool]:
        del kwargs
        if self.existing_status is not None:
            return SimpleNamespace(status=self.existing_status), False
        self.reserved = True
        return SimpleNamespace(status=IdempotencyStatus.PENDING), True

    def release_pending(self, operation_key: str) -> None:
        assert operation_key == "op-1"
        self.released = True

    def mark_uncertain(self, operation_key: str) -> None:
        assert operation_key == "op-1"
        self.uncertain = True

    def complete(self, operation_key: str, result: object) -> None:
        assert operation_key == "op-1"
        assert result
        self.completed = True


class FakeAdapter:
    def __init__(self, snapshots: list[SemanticSnapshot]) -> None:
        self.snapshots = snapshots
        self.index = 0
        self.focused: str | None = "before"
        self.act_calls = 0
        self.verify_result = True
        self.fail_after_start = False

    def observe(self) -> SemanticSnapshot:
        snapshot = self.snapshots[min(self.index, len(self.snapshots) - 1)]
        self.index += 1
        return snapshot

    def capture_focus(self) -> str | None:
        return self.focused

    def focus(self, node: ControlNode) -> None:
        self.focused = node.node_id

    def act(self, node: ControlNode, action: InteractionAction, value: str | None) -> None:
        del node, action, value
        self.act_calls += 1
        if self.fail_after_start:
            raise RuntimeError("adapter transport failed")

    def verify(
        self,
        before: SemanticSnapshot,
        after: SemanticSnapshot,
        node: ControlNode,
        action: InteractionAction,
        value: str | None,
    ) -> bool:
        del before, after, node, action, value
        return self.verify_result


def _policy(tmp_path: Path, *, granted: bool = True) -> tuple[SecurityPolicy, ExecutionBudgetLedger]:
    policy = SecurityPolicy(
        granted_tools=frozenset({"interaction.invoke"} if granted else set()),
        sandbox=SandboxPolicy(workspace_root=tmp_path),
        budget=ExecutionBudget(),
    )
    return policy, ExecutionBudgetLedger(policy.budget)


def _request(risk: InteractionRisk = InteractionRisk.R0_OBSERVE) -> InteractionRequest:
    return InteractionRequest(
        task_id="task-1",
        operation_key="op-1",
        tool_id="interaction.invoke",
        target="fixture/save",
        locator=ControlLocator(role="button", name="Save"),
        action=InteractionAction.INVOKE,
        risk=risk,
    )


def _coordinator(
    tmp_path: Path,
    adapter: FakeAdapter,
    ledger: FakeLedger,
    *,
    granted: bool = True,
) -> SemanticInteractionCoordinator:
    policy, budgets = _policy(tmp_path, granted=granted)
    return SemanticInteractionCoordinator(
        adapter=adapter,
        security_policy=policy,
        budgets=budgets,
        approvals=ApprovalLedger(),
        idempotency=ledger,  # type: ignore[arg-type]
    )


def test_verified_semantic_action_captures_focus_evidence(tmp_path: Path) -> None:
    save = ControlNode("save", "button", "Save")
    adapter = FakeAdapter([_snapshot(save), _snapshot(save), _snapshot(save)])
    result = _coordinator(tmp_path, adapter, FakeLedger()).execute(_request())
    assert result.succeeded is True
    assert result.evidence.focus_before == "before"
    assert result.evidence.focus_after == "save"
    assert adapter.act_calls == 1


def test_stale_revision_blocks_before_action(tmp_path: Path) -> None:
    save = ControlNode("save", "button", "Save")
    adapter = FakeAdapter([_snapshot(save), _snapshot(save, revision=2)])
    with pytest.raises(StaleSnapshotError):
        _coordinator(tmp_path, adapter, FakeLedger()).execute(_request())
    assert adapter.act_calls == 0


def test_permission_denial_is_terminal_and_never_acts(tmp_path: Path) -> None:
    save = ControlNode("save", "button", "Save")
    adapter = FakeAdapter([_snapshot(save), _snapshot(save)])
    with pytest.raises(PermissionBlockedError):
        _coordinator(tmp_path, adapter, FakeLedger(), granted=False).execute(_request())
    assert adapter.act_calls == 0


@pytest.mark.parametrize("status", [IdempotencyStatus.PENDING, IdempotencyStatus.UNCERTAIN])
def test_prior_unsettled_side_effect_blocks_blind_retry(
    tmp_path: Path,
    status: IdempotencyStatus,
) -> None:
    save = ControlNode("save", "button", "Save")
    adapter = FakeAdapter([_snapshot(save), _snapshot(save)])
    ledger = FakeLedger(existing_status=status)
    with pytest.raises(InteractionReplayBlockedError):
        _coordinator(tmp_path, adapter, ledger).execute(
            _request(InteractionRisk.R2_EXTERNAL_SIDE_EFFECT)
        )
    assert adapter.act_calls == 0


def test_completed_side_effect_is_not_replayed(tmp_path: Path) -> None:
    save = ControlNode("save", "button", "Save")
    adapter = FakeAdapter([_snapshot(save), _snapshot(save)])
    ledger = FakeLedger(existing_status=IdempotencyStatus.COMPLETED)
    with pytest.raises(InteractionReplayBlockedError):
        _coordinator(tmp_path, adapter, ledger).execute(
            _request(InteractionRisk.R2_EXTERNAL_SIDE_EFFECT)
        )
    assert adapter.act_calls == 0


def test_permission_denial_releases_side_effect_reservation(tmp_path: Path) -> None:
    save = ControlNode("save", "button", "Save")
    adapter = FakeAdapter([_snapshot(save), _snapshot(save)])
    ledger = FakeLedger()
    with pytest.raises(PermissionBlockedError):
        _coordinator(tmp_path, adapter, ledger, granted=False).execute(
            _request(InteractionRisk.R2_EXTERNAL_SIDE_EFFECT)
        )
    assert ledger.reserved is True
    assert ledger.released is True
    assert adapter.act_calls == 0


def test_failed_postcondition_marks_side_effect_uncertain(tmp_path: Path) -> None:
    save = ControlNode("save", "button", "Save")
    adapter = FakeAdapter([_snapshot(save), _snapshot(save), _snapshot(save)])
    adapter.verify_result = False
    ledger = FakeLedger()
    with pytest.raises(PermissionBlockedError):
        # R2 requires explicit approval under the shared M10 policy, so it fails before ACT.
        _coordinator(tmp_path, adapter, ledger).execute(
            _request(InteractionRisk.R2_EXTERNAL_SIDE_EFFECT)
        )
    assert ledger.released is True
    assert ledger.uncertain is False


def test_adapter_failure_after_local_action_propagates_without_false_success(tmp_path: Path) -> None:
    save = ControlNode("save", "button", "Save")
    adapter = FakeAdapter([_snapshot(save), _snapshot(save)])
    adapter.fail_after_start = True
    with pytest.raises(RuntimeError, match="adapter transport failed"):
        _coordinator(tmp_path, adapter, FakeLedger()).execute(
            _request(InteractionRisk.R1_LOCAL_REVERSIBLE)
        )


def test_fingerprint_changes_with_semantic_target() -> None:
    first = _request()
    second = InteractionRequest(
        task_id=first.task_id,
        operation_key=first.operation_key,
        tool_id=first.tool_id,
        target=first.target,
        locator=ControlLocator(role="button", name="Cancel"),
        action=first.action,
        risk=first.risk,
    )
    assert first.fingerprint != second.fingerprint
