from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from nika_core.interaction import (
    AmbiguousTargetError,
    ApplicationIdentity,
    ControlLocator,
    ControlNode,
    InteractionAction,
    InteractionTarget,
    PermissionBlockedError,
    SemanticSnapshot,
    StaleSnapshotError,
    TargetNotFoundError,
)
from nika_core.interaction.orchestration import (
    InteractionReplayBlockedError,
    InteractionRequest,
    InteractionRisk,
    SemanticInteractionCoordinator,
)
from nika_core.interaction.resolver import resolve_strict
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


def _policy(
    tmp_path: Path,
    *,
    granted: bool = True,
) -> tuple[SecurityPolicy, ExecutionBudgetLedger]:
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


def test_adapter_failure_after_local_action_propagates_without_false_success(
    tmp_path: Path,
) -> None:
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


@pytest.mark.parametrize("enabled,visible", [(False, True), (True, False), (False, False)])
@pytest.mark.parametrize("changed_at_second_observation", [False, True])
def test_non_actionable_control_never_reaches_authorization_or_adapter(
    tmp_path: Path, enabled: bool, visible: bool, changed_at_second_observation: bool
) -> None:
    actionable = ControlNode("save", "button", "Save")
    blocked = ControlNode("save", "button", "Save", enabled=enabled, visible=visible)
    snapshots = (
        [_snapshot(actionable), _snapshot(blocked)]
        if changed_at_second_observation
        else [_snapshot(blocked), _snapshot(blocked)]
    )
    adapter = FakeAdapter(snapshots)
    ledger = FakeLedger()
    with pytest.raises(StaleSnapshotError, match="disabled or hidden"):
        _coordinator(tmp_path, adapter, ledger).execute(
            _request(InteractionRisk.R2_EXTERNAL_SIDE_EFFECT)
        )
    assert adapter.act_calls == 0
    assert ledger.reserved is False


@pytest.mark.parametrize(
    "original,changed",
    [
        (ControlNode("save", "button", "Save"), ControlNode("replacement", "button", "Save")),
        (ControlNode("save", "button", "Save", value="old"),
         ControlNode("save", "button", "Save", value="new")),
        (ControlNode("save", "button", "Save", attributes=(("label", "Save"),)),
         ControlNode("save", "button", "Save", attributes=(("label", "Publish"),))),
    ],
)
def test_semantic_drift_at_unchanged_revision_blocks_before_effect(
    tmp_path: Path, original: ControlNode, changed: ControlNode
) -> None:
    adapter = FakeAdapter([_snapshot(original), _snapshot(changed)])
    ledger = FakeLedger()
    with pytest.raises(StaleSnapshotError, match="target changed"):
        _coordinator(tmp_path, adapter, ledger).execute(
            _request(InteractionRisk.R2_EXTERNAL_SIDE_EFFECT)
        )
    assert adapter.act_calls == 0
    assert ledger.reserved is False


def test_focus_and_geometry_changes_do_not_replace_semantic_target(tmp_path: Path) -> None:
    original = ControlNode("save", "button", "Save", bounds=(0, 0, 10, 10))
    focused = ControlNode(
        "save", "button", "Save", focused=True, bounds=(10, 10, 20, 20)
    )
    adapter = FakeAdapter([_snapshot(original), _snapshot(focused), _snapshot(focused)])
    assert _coordinator(tmp_path, adapter, FakeLedger()).execute(_request()).succeeded
    assert adapter.act_calls == 1


def test_resolution_errors_never_echo_sensitive_locator_contents() -> None:
    sensitive = "NIKA_PRIVATE_LOCATOR_CANARY"
    with pytest.raises(TargetNotFoundError) as missing:
        resolve_strict(_snapshot(), ControlLocator(name=sensitive))
    assert sensitive not in str(missing.value)

    duplicate = ControlNode("save", "button", sensitive)
    with pytest.raises(AmbiguousTargetError) as ambiguous:
        resolve_strict(_snapshot(duplicate, duplicate), ControlLocator(name=sensitive))
    assert sensitive not in str(ambiguous.value)


@pytest.mark.parametrize("enabled,visible", [("false", True), (True, "false"), (1, True)])
def test_non_boolean_actionability_cannot_authorize_effect(
    tmp_path: Path, enabled: object, visible: object
) -> None:
    # Runtime adapter DTOs are not protected by dataclass type annotations.
    forged = ControlNode("save", "button", "Save", enabled=enabled, visible=visible)
    adapter = FakeAdapter([_snapshot(forged), _snapshot(forged)])
    ledger = FakeLedger()
    with pytest.raises(StaleSnapshotError, match="disabled or hidden"):
        _coordinator(tmp_path, adapter, ledger).execute(
            _request(InteractionRisk.R2_EXTERNAL_SIDE_EFFECT)
        )
    assert adapter.act_calls == 0
    assert ledger.reserved is False


def test_reused_mutated_control_carrier_is_fenced_before_effect(tmp_path: Path) -> None:
    shared = ControlNode("save", "button", "Save", value="draft")

    class MutatingAdapter(FakeAdapter):
        def observe(self) -> SemanticSnapshot:
            if self.index == 1:
                object.__setattr__(shared, "value", "publish")
            return super().observe()

    adapter = MutatingAdapter([_snapshot(shared), _snapshot(shared)])
    ledger = FakeLedger()
    with pytest.raises(StaleSnapshotError, match="target changed"):
        _coordinator(tmp_path, adapter, ledger).execute(
            _request(InteractionRisk.R2_EXTERNAL_SIDE_EFFECT)
        )
    assert adapter.act_calls == 0
    assert ledger.reserved is False


@pytest.mark.parametrize(
    "changed",
    [
        ControlNode("save", "button", "Save", enabled=False),
        ControlNode("save", "button", "Save", visible=False),
        ControlNode("save", "button", "Save", value="unexpected"),
        ControlNode("replacement", "button", "Save"),
    ],
)
def test_focus_transition_cannot_redirect_or_disable_the_action(
    tmp_path: Path, changed: ControlNode
) -> None:
    save = ControlNode("save", "button", "Save")

    class ChangingFocusAdapter(FakeAdapter):
        def focus(self, node: ControlNode) -> None:
            super().focus(node)
            self.snapshots.append(_snapshot(changed))

    adapter = ChangingFocusAdapter([_snapshot(save), _snapshot(save)])
    with pytest.raises(StaleSnapshotError):
        _coordinator(tmp_path, adapter, FakeLedger()).execute(_request())
    assert adapter.act_calls == 0


def test_focus_navigation_rejects_stale_target_before_effect(tmp_path: Path) -> None:
    save = ControlNode("save", "button", "Save")

    class NavigatingFocusAdapter(FakeAdapter):
        def focus(self, node: ControlNode) -> None:
            super().focus(node)
            self.snapshots.append(_snapshot(save, generation=2))

    adapter = NavigatingFocusAdapter([_snapshot(save), _snapshot(save)])
    with pytest.raises(StaleSnapshotError, match="changed after focus"):
        _coordinator(tmp_path, adapter, FakeLedger()).execute(_request())
    assert adapter.act_calls == 0


def test_request_mutation_during_observation_cannot_swap_authorized_action(
    tmp_path: Path
) -> None:
    request = _request()
    save = ControlNode("save", "button", "Save")
    seen: list[tuple[str, InteractionAction]] = []

    class SwappingAdapter(FakeAdapter):
        def observe(self) -> SemanticSnapshot:
            if self.index == 1:
                object.__setattr__(request, "action", InteractionAction.SET_VALUE)
                object.__setattr__(request.locator, "name", "Delete")
                object.__setattr__(request, "value", "unapproved replacement")
            return super().observe()

        def act(self, node: ControlNode, action: InteractionAction, value: str | None) -> None:
            seen.append((node.name, action))
            super().act(node, action, value)

    adapter = SwappingAdapter([_snapshot(save), _snapshot(save), _snapshot(save)])
    assert _coordinator(tmp_path, adapter, FakeLedger()).execute(request).succeeded
    assert seen == [("Save", InteractionAction.INVOKE)]


def test_request_risk_mutation_during_observation_cannot_bypass_approval(
    tmp_path: Path
) -> None:
    request = _request(InteractionRisk.R2_EXTERNAL_SIDE_EFFECT)
    save = ControlNode("save", "button", "Save")

    class DowngradingAdapter(FakeAdapter):
        def observe(self) -> SemanticSnapshot:
            object.__setattr__(request, "risk", InteractionRisk.R0_OBSERVE)
            return super().observe()

    adapter = DowngradingAdapter([_snapshot(save), _snapshot(save)])
    ledger = FakeLedger()
    with pytest.raises(PermissionBlockedError):
        _coordinator(tmp_path, adapter, ledger).execute(request)
    assert adapter.act_calls == 0
    assert ledger.reserved is True
    assert ledger.released is True
