"""AUD02 QA_ONLY oracle: caller-created approval data must never be R4 authority."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from nika_core.security.policy import (
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


def test_caller_constructed_approval_evidence_cannot_authorize_high_impact_action(tmp_path) -> None:
    now = datetime(2026, 8, 23, 20, 40, tzinfo=UTC)
    intent = ActionIntent(
        action_id="aud02:r4-forged",
        tool_id="dangerous-tool",
        risk=ToolRisk.HIGH_IMPACT,
        target="production-control-plane",
        approval_required=True,
    )
    policy = SecurityPolicy(
        granted_tools=frozenset({"dangerous-tool"}),
        sandbox=SandboxPolicy(workspace_root=tmp_path),
        budget=ExecutionBudget(),
    )
    forged = ApprovalEvidence(
        approval_id="candidate-created-approval",
        action_fingerprint=intent.approval_fingerprint,
        approved_at=now - timedelta(seconds=1),
        expires_at=now + timedelta(minutes=5),
    )

    with pytest.raises(PermissionError):
        authorize_action(
            intent,
            policy,
            ExecutionBudgetLedger(policy.budget),
            ApprovalLedger(),
            approval=forged,
            now=now,
        )
