from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from nika_core.security import (
    ActionIntent,
    ApprovalEvidence,
    ApprovalLedger,
    ExecutionBudget,
    ExecutionBudgetLedger,
    SandboxPolicy,
    SecurityPolicy,
    authorize_action,
)
from nika_core.tools import ToolRisk


def _approval(intent: ActionIntent) -> ApprovalEvidence:
    now = datetime(2026, 8, 25, 0, 0, tzinfo=UTC)
    return ApprovalEvidence(
        approval_id="qa-executable-scope-approval",
        action_fingerprint=intent.approval_fingerprint,
        approved_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=4),
    )


def test_path_scope_denial_consumes_neither_approval_nor_process_budget(tmp_path: Path) -> None:
    intent = ActionIntent(
        action_id="qa-m10-executable-scope",
        tool_id="danger.execute",
        risk=ToolRisk.HIGH_IMPACT,
        target="exact executable authority",
        executable="/untrusted/bin/pytest",
    )
    approval = _approval(intent)
    approvals = ApprovalLedger()
    budget = ExecutionBudget(max_process_launches=1)
    budgets = ExecutionBudgetLedger(budget)
    now = datetime(2026, 8, 25, 0, 0, tzinfo=UTC)

    basename_only = SecurityPolicy(
        granted_tools=frozenset({"danger.execute"}),
        sandbox=SandboxPolicy(
            workspace_root=tmp_path / "workspace",
            allowed_executables=("pytest",),
        ),
        budget=budget,
    )
    with pytest.raises(PermissionError, match="process executable"):
        authorize_action(
            intent,
            basename_only,
            budgets,
            approvals,
            approval=approval,
            now=now,
        )
    assert budgets.process_launches == 0

    exact_path = SecurityPolicy(
        granted_tools=frozenset({"danger.execute"}),
        sandbox=SandboxPolicy(
            workspace_root=tmp_path / "workspace",
            allowed_executables=(intent.executable,),
        ),
        budget=budget,
    )
    decision = authorize_action(
        intent,
        exact_path,
        budgets,
        approvals,
        approval=approval,
        now=now,
    )

    assert decision.approved is True
    assert budgets.process_launches == 1
