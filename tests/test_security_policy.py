from datetime import UTC, datetime, timedelta
from pathlib import Path

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

_NOW = datetime(2026, 8, 18, 19, 0, tzinfo=UTC)


def _authority() -> ApprovalAuthority:
    return ApprovalAuthority(issuer_id="security-policy-test", secret=b"s" * 32)


def _policy(tmp_path: Path, authority: ApprovalAuthority | None = None) -> SecurityPolicy:
    return SecurityPolicy(
        granted_tools=frozenset({"files.write", "browser.read", "process.test", "danger.execute"}),
        sandbox=SandboxPolicy(
            workspace_root=tmp_path / "workspace",
            writable_roots=("artifacts", "worktrees"),
            allowed_network_hosts=("example.test",),
            allowed_executables=("pytest", "python.exe"),
        ),
        budget=ExecutionBudget(
            max_write_bytes=100,
            max_network_calls=2,
            max_process_launches=1,
        ),
        approval_verifier=None if authority is None else authority.verifier(),
    )


def _approval(
    authority: ApprovalAuthority,
    intent: ActionIntent,
    *,
    requested_at: datetime = _NOW,
    approved_at: datetime = _NOW,
    ttl: timedelta = timedelta(minutes=5),
) -> ApprovalEvidence:
    request = authority.request(
        intent,
        reason="explicit test approval",
        now=requested_at,
        ttl=ttl,
    )
    return authority.approve(request.request_id, now=approved_at)


def test_workspace_write_is_confined_and_budgeted(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    ledger = ExecutionBudgetLedger(policy.budget)
    intent = ActionIntent(
        action_id="write-1",
        tool_id="files.write",
        risk=ToolRisk.LOCAL_WRITE,
        target="artifact",
        write_path="artifacts/result.txt",
        write_bytes=60,
    )
    result = authorize_action(intent, policy, ledger, ApprovalLedger())
    assert result.resolved_write_path == (
        tmp_path / "workspace" / "artifacts" / "result.txt"
    ).resolve()
    assert ledger.write_bytes == 60

    with pytest.raises(PermissionError, match="budget"):
        authorize_action(
            ActionIntent(
                action_id="write-2",
                tool_id="files.write",
                risk=ToolRisk.LOCAL_WRITE,
                target="artifact",
                write_path="artifacts/other.txt",
                write_bytes=50,
            ),
            policy,
            ledger,
            ApprovalLedger(),
        )


def test_traversal_and_unapproved_write_root_fail_closed(tmp_path: Path) -> None:
    sandbox = _policy(tmp_path).sandbox
    with pytest.raises(PermissionError, match="workspace-relative"):
        sandbox.resolve_write("../secret.txt")
    with pytest.raises(PermissionError, match="outside allowed roots"):
        sandbox.resolve_write("private/secret.txt")


def test_network_and_process_require_explicit_allowlist(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    ledger = ExecutionBudgetLedger(policy.budget)
    approvals = ApprovalLedger()
    authorize_action(
        ActionIntent(
            action_id="net-1",
            tool_id="browser.read",
            risk=ToolRisk.READ_ONLY,
            target="page",
            network_host="EXAMPLE.TEST.",
        ),
        policy,
        ledger,
        approvals,
    )
    authorize_action(
        ActionIntent(
            action_id="proc-1",
            tool_id="process.test",
            risk=ToolRisk.LOCAL_WRITE,
            target="tests",
            executable="/usr/bin/pytest",
        ),
        policy,
        ledger,
        approvals,
    )
    with pytest.raises(PermissionError, match="network host"):
        policy.sandbox.authorize_network("evil.example")
    with pytest.raises(PermissionError, match="process executable"):
        policy.sandbox.authorize_executable("powershell.exe")


def test_high_impact_requires_trusted_verifier(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    intent = ActionIntent(
        action_id="danger-no-verifier",
        tool_id="danger.execute",
        risk=ToolRisk.HIGH_IMPACT,
        target="named operation",
    )
    with pytest.raises(PermissionError, match="trusted approval verifier"):
        authorize_action(
            intent,
            policy,
            ExecutionBudgetLedger(policy.budget),
            ApprovalLedger(),
            now=_NOW,
        )


def test_high_impact_approval_is_exact_expiring_and_single_use(tmp_path: Path) -> None:
    authority = _authority()
    policy = _policy(tmp_path, authority)
    intent = ActionIntent(
        action_id="danger-1",
        tool_id="danger.execute",
        risk=ToolRisk.HIGH_IMPACT,
        target="named operation",
    )
    ledger = ExecutionBudgetLedger(policy.budget)
    approvals = ApprovalLedger()

    with pytest.raises(PermissionError, match="explicit approval"):
        authorize_action(intent, policy, ledger, approvals, now=_NOW)

    evidence = _approval(authority, intent)
    authorize_action(intent, policy, ledger, approvals, approval=evidence, now=_NOW)
    with pytest.raises(PermissionError, match="already used"):
        authorize_action(intent, policy, ledger, approvals, approval=evidence, now=_NOW)

    other = ActionIntent(
        action_id="danger-2",
        tool_id="danger.execute",
        risk=ToolRisk.HIGH_IMPACT,
        target="different operation",
    )
    with pytest.raises(PermissionError, match="exact action"):
        authorize_action(
            other,
            policy,
            ExecutionBudgetLedger(policy.budget),
            ApprovalLedger(),
            approval=_approval(authority, intent),
            now=_NOW,
        )


def test_expired_approval_is_rejected(tmp_path: Path) -> None:
    authority = _authority()
    policy = _policy(tmp_path, authority)
    intent = ActionIntent(
        action_id="danger-expired",
        tool_id="danger.execute",
        risk=ToolRisk.HIGH_IMPACT,
        target="operation",
    )
    approval = _approval(
        authority,
        intent,
        requested_at=_NOW - timedelta(minutes=4),
        approved_at=_NOW - timedelta(minutes=1),
        ttl=timedelta(minutes=5),
    )
    with pytest.raises(PermissionError, match="not currently valid"):
        authorize_action(
            intent,
            policy,
            ExecutionBudgetLedger(policy.budget),
            ApprovalLedger(),
            approval=approval,
            now=approval.expires_at,
        )


def test_ungranted_tool_fails_before_side_effect_budget(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    ledger = ExecutionBudgetLedger(policy.budget)
    with pytest.raises(PermissionError, match="not granted"):
        authorize_action(
            ActionIntent(
                action_id="unknown-1",
                tool_id="unknown.tool",
                risk=ToolRisk.READ_ONLY,
                target="nothing",
                network_host="example.test",
            ),
            policy,
            ledger,
            ApprovalLedger(),
        )
    assert ledger.network_calls == 0
