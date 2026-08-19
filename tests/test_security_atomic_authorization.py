from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier

import pytest

from nika_core.security import (
    ActionIntent,
    ApprovalAuthority,
    ApprovalEvidence,
    ApprovalLedger,
    ExecutionBudget,
    ExecutionBudgetLedger,
    SandboxPolicy,
    SecurityPolicy,
    authorize_action,
)
from nika_core.tools import ToolRisk

_NOW = datetime(2026, 8, 19, 17, 0, tzinfo=UTC)


def _authority() -> ApprovalAuthority:
    return ApprovalAuthority(issuer_id="test-desktop", secret=b"a" * 32)


def _policy(
    tmp_path: Path,
    authority: ApprovalAuthority,
    *,
    max_write_bytes: int = 1,
) -> SecurityPolicy:
    return SecurityPolicy(
        granted_tools=frozenset({"danger.execute"}),
        sandbox=SandboxPolicy(
            workspace_root=tmp_path / "workspace",
            writable_roots=("artifacts",),
        ),
        budget=ExecutionBudget(max_write_bytes=max_write_bytes),
        approval_verifier=authority.verifier(),
    )


def _intent(action_id: str) -> ActionIntent:
    return ActionIntent(
        action_id=action_id,
        tool_id="danger.execute",
        risk=ToolRisk.HIGH_IMPACT,
        target=f"named operation {action_id}",
        write_path=f"artifacts/{action_id}.txt",
        write_bytes=1,
    )


def _approval(authority: ApprovalAuthority, intent: ActionIntent) -> ApprovalEvidence:
    request = authority.request(intent, reason="test explicit human approval", now=_NOW)
    return authority.approve(request.request_id, now=_NOW)


def test_budget_rejection_does_not_consume_one_time_approval(tmp_path: Path) -> None:
    authority = _authority()
    policy = _policy(tmp_path, authority, max_write_bytes=0)
    intent = _intent("budget-denied")
    approval = _approval(authority, intent)
    approvals = ApprovalLedger()

    with pytest.raises(PermissionError, match="write budget"):
        authorize_action(
            intent,
            policy,
            ExecutionBudgetLedger(policy.budget),
            approvals,
            approval=approval,
            now=_NOW,
        )

    approvals.consume(intent, approval, now=_NOW)
    with pytest.raises(PermissionError, match="already used"):
        approvals.consume(intent, approval, now=_NOW)


def test_missing_approval_does_not_reserve_budget(tmp_path: Path) -> None:
    authority = _authority()
    policy = _policy(tmp_path, authority)
    intent = _intent("approval-missing")
    budgets = ExecutionBudgetLedger(policy.budget)

    with pytest.raises(PermissionError, match="explicit approval"):
        authorize_action(intent, policy, budgets, ApprovalLedger(), now=_NOW)

    assert budgets.write_bytes == 0
    assert budgets.network_calls == 0
    assert budgets.process_launches == 0


def test_invalid_approval_does_not_reserve_budget(tmp_path: Path) -> None:
    authority = _authority()
    policy = _policy(tmp_path, authority)
    intent = _intent("approval-mismatch")
    other = _intent("other-action")
    budgets = ExecutionBudgetLedger(policy.budget)

    with pytest.raises(PermissionError, match="exact action"):
        authorize_action(
            intent,
            policy,
            budgets,
            ApprovalLedger(),
            approval=_approval(authority, other),
            now=_NOW,
        )

    assert budgets.write_bytes == 0


def test_budget_and_approval_commit_together_under_competing_threads(tmp_path: Path) -> None:
    authority = _authority()
    policy = _policy(tmp_path, authority, max_write_bytes=1)
    budgets = ExecutionBudgetLedger(policy.budget)
    approvals = ApprovalLedger()
    intents = (_intent("race-a"), _intent("race-b"))
    evidence = {intent.action_id: _approval(authority, intent) for intent in intents}
    barrier = Barrier(2)

    def attempt(intent: ActionIntent) -> tuple[str, bool, str | None]:
        barrier.wait()
        try:
            authorize_action(
                intent,
                policy,
                budgets,
                approvals,
                approval=evidence[intent.action_id],
                now=_NOW,
            )
        except PermissionError as exc:
            return intent.action_id, False, str(exc)
        return intent.action_id, True, None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(attempt, intents))

    winners = [action_id for action_id, ok, _ in results if ok]
    losers = [(action_id, error) for action_id, ok, error in results if not ok]
    assert len(winners) == 1
    assert len(losers) == 1
    assert losers[0][1] == "filesystem write budget exceeded"
    assert budgets.write_bytes == 1

    loser_id = losers[0][0]
    loser_intent = next(intent for intent in intents if intent.action_id == loser_id)
    fresh_budget = ExecutionBudgetLedger(policy.budget)
    authorize_action(
        loser_intent,
        policy,
        fresh_budget,
        approvals,
        approval=evidence[loser_id],
        now=_NOW,
    )
    assert fresh_budget.write_bytes == 1


def test_success_commits_budget_and_consumes_approval_once(tmp_path: Path) -> None:
    authority = _authority()
    policy = _policy(tmp_path, authority)
    intent = _intent("success")
    approval = _approval(authority, intent)
    budgets = ExecutionBudgetLedger(policy.budget)
    approvals = ApprovalLedger()

    decision = authorize_action(
        intent,
        policy,
        budgets,
        approvals,
        approval=approval,
        now=_NOW,
    )

    assert decision.approved is True
    assert budgets.write_bytes == 1
    with pytest.raises(PermissionError, match="already used"):
        approvals.consume(intent, approval, now=_NOW)
