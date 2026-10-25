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
    InteractionRequest,
    InteractionRisk,
    SemanticInteractionCoordinator,
)
from nika_core.runtime.idempotency import IdempotencyStatus
from nika_core.security.approval import ApprovalAuthority
from nika_core.security.policy import (
    ApprovalLedger,
    ExecutionBudget,
    ExecutionBudgetLedger,
    SandboxPolicy,
    SecurityPolicy,
)


@dataclass
class _Ledger:
    reserved: bool = False
    released: bool = False
    completed: bool = False

    def reserve_once(self, **kwargs: object) -> tuple[object, bool]:
        assert kwargs["task_id"] == "task-1"
        self.reserved = True
        return SimpleNamespace(status=IdempotencyStatus.PENDING), True

    def release_pending(self, operation_key: str) -> None:
        assert operation_key == "op-1"
        self.released = True

    def mark_uncertain(self, operation_key: str) -> None:
        raise AssertionError(f"unexpected uncertain effect: {operation_key}")

    def complete(self, operation_key: str, result: object) -> None:
        assert operation_key == "op-1"
        assert result
        self.completed = True


class _Adapter:
    def __init__(self, snapshots: list[SemanticSnapshot]) -> None:
        self.snapshots = snapshots
        self.index = 0
        self.focused: str | None = "before"
        self.act_calls = 0

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


def _snapshot(*nodes: ControlNode, generation: int = 1, revision: int = 1) -> SemanticSnapshot:
    return SemanticSnapshot(
        target=InteractionTarget(application=ApplicationIdentity("fixture.exe", 42, 100)),
        generation=generation,
        revision=revision,
        controls=tuple(nodes),
    )


def _request(
    *,
    risk: InteractionRisk = InteractionRisk.R2_EXTERNAL_SIDE_EFFECT,
    action: InteractionAction = InteractionAction.INVOKE,
    value: str | None = None,
    locator: ControlLocator | None = None,
) -> InteractionRequest:
    return InteractionRequest(
        task_id="task-1",
        operation_key="op-1",
        tool_id="interaction.invoke",
        target="fixture/save",
        locator=locator or ControlLocator(role="button", name="Save"),
        action=action,
        risk=risk,
        value=value,
        project_id="project-1",
    )


def _coordinator(
    tmp_path: Path,
    adapter: _Adapter,
    ledger: _Ledger,
    *,
    authority: ApprovalAuthority | None = None,
) -> SemanticInteractionCoordinator:
    policy = SecurityPolicy(
        granted_tools=frozenset({"interaction.invoke"}),
        sandbox=SandboxPolicy(workspace_root=tmp_path),
        budget=ExecutionBudget(),
        approval_verifier=None if authority is None else authority.verifier(),
    )
    return SemanticInteractionCoordinator(
        adapter=adapter,
        security_policy=policy,
        budgets=ExecutionBudgetLedger(policy.budget),
        approvals=ApprovalLedger(),
        idempotency=ledger,  # type: ignore[arg-type]
    )


def test_trusted_approval_executes_exact_external_semantic_effect(tmp_path: Path) -> None:
    save = ControlNode("save", "button", "Save")
    adapter = _Adapter([_snapshot(save), _snapshot(save), _snapshot(save), _snapshot(save)])
    ledger = _Ledger()
    authority = ApprovalAuthority(issuer_id="interaction-test-host", secret=b"a" * 32)
    request = _request()

    view = authority.request(request.approval_intent)
    assert view.task_id == request.task_id
    assert view.project_id == request.project_id
    assert view.effect_id == request.operation_key
    evidence = authority.approve(view.request_id)

    result = _coordinator(
        tmp_path,
        adapter,
        ledger,
        authority=authority,
    ).execute(request, approval=evidence)

    assert result.succeeded is True
    assert adapter.act_calls == 1
    assert ledger.reserved is True
    assert ledger.completed is True
    assert ledger.released is False


def test_trusted_approval_rejects_semantic_effect_swap_before_adapter(tmp_path: Path) -> None:
    save = ControlNode("save", "button", "Save")
    adapter = _Adapter([_snapshot(save), _snapshot(save)])
    ledger = _Ledger()
    authority = ApprovalAuthority(issuer_id="interaction-test-host", secret=b"b" * 32)
    approved = _request()
    evidence = authority.approve(authority.request(approved.approval_intent).request_id)
    changed = _request(
        action=InteractionAction.SET_VALUE,
        value="replacement",
    )

    with pytest.raises(PermissionBlockedError, match="exact action"):
        _coordinator(
            tmp_path,
            adapter,
            ledger,
            authority=authority,
        ).execute(changed, approval=evidence)

    assert adapter.act_calls == 0
    assert ledger.reserved is True
    assert ledger.released is True
    assert ledger.completed is False


@pytest.mark.parametrize("mutation", ["generation", "revision", "target"])
def test_reused_mutated_snapshot_is_fenced_before_reservation(
    tmp_path: Path,
    mutation: str,
) -> None:
    save = ControlNode("save", "button", "Save")
    shared = _snapshot(save)

    class _MutatingSnapshotAdapter(_Adapter):
        def observe(self) -> SemanticSnapshot:
            if self.index == 1:
                if mutation == "generation":
                    object.__setattr__(shared, "generation", 2)
                elif mutation == "revision":
                    object.__setattr__(shared, "revision", 2)
                else:
                    assert shared.target.application is not None
                    object.__setattr__(shared.target.application, "pid", 99)
            return super().observe()

    adapter = _MutatingSnapshotAdapter([shared, shared])
    ledger = _Ledger()
    with pytest.raises(StaleSnapshotError):
        _coordinator(tmp_path, adapter, ledger).execute(
            _request(risk=InteractionRisk.R0_OBSERVE)
        )

    assert adapter.act_calls == 0
    assert ledger.reserved is False


def test_reused_snapshot_mutated_during_focus_is_fenced_before_effect(tmp_path: Path) -> None:
    save = ControlNode("save", "button", "Save")
    first = _snapshot(save)
    shared = _snapshot(save)

    class _FocusMutationAdapter(_Adapter):
        def observe(self) -> SemanticSnapshot:
            if self.index == 2:
                object.__setattr__(shared, "generation", 2)
            return super().observe()

    adapter = _FocusMutationAdapter([first, shared, shared])
    ledger = _Ledger()
    with pytest.raises(StaleSnapshotError, match="changed after focus"):
        _coordinator(tmp_path, adapter, ledger).execute(
            _request(risk=InteractionRisk.R0_OBSERVE)
        )

    assert adapter.act_calls == 0
    assert ledger.reserved is False


def test_approval_fingerprint_binds_locator_and_project() -> None:
    request = _request()
    changed_locator = _request(locator=ControlLocator(role="button", name="Cancel"))
    changed_project = InteractionRequest(
        task_id=request.task_id,
        operation_key=request.operation_key,
        tool_id=request.tool_id,
        target=request.target,
        locator=request.locator,
        action=request.action,
        risk=request.risk,
        value=request.value,
        project_id="project-2",
    )

    assert request.fingerprint != changed_locator.fingerprint
    assert request.fingerprint != changed_project.fingerprint
