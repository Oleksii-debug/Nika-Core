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


def _policy(tmp_path: Path) -> SecurityPolicy:
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
    )


def _approval(intent: ActionIntent, *, approval_id: str = "approval-1") -> ApprovalEvidence:
    now = datetime(2026, 8, 18, 19, 0, tzinfo=UTC)
    return ApprovalEvidence(
        approval_id=approval_id,
        action_fingerprint=intent.approval_fingerprint,
        approved_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(minutes=4),
    )


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
            executable="pytest",
        ),
        policy,
        ledger,
        approvals,
    )
    with pytest.raises(PermissionError, match="network host"):
        policy.sandbox.authorize_network("evil.example")
    with pytest.raises(PermissionError, match="process executable"):
        policy.sandbox.authorize_executable("powershell.exe")


def test_executable_name_grant_cannot_authorize_path_qualified_alias(tmp_path: Path) -> None:
    sandbox = SandboxPolicy(
        workspace_root=tmp_path,
        allowed_executables=("pytest",),
    )
    sandbox.authorize_executable("pytest")

    for executable in ("/tmp/pytest", r"C:\Temp\pytest"):
        with pytest.raises(PermissionError, match="process executable"):
            sandbox.authorize_executable(executable)


def test_explicit_executable_path_requires_exact_path_scope(tmp_path: Path) -> None:
    sandbox = SandboxPolicy(
        workspace_root=tmp_path,
        allowed_executables=("/usr/bin/pytest", r"C:\Tools\pytest.exe"),
    )
    sandbox.authorize_executable("/usr/bin/pytest")
    sandbox.authorize_executable(r"c:\tools\PYTEST.EXE")

    for executable in ("/tmp/pytest", r"C:\Other\pytest.exe"):
        with pytest.raises(PermissionError, match="process executable"):
            sandbox.authorize_executable(executable)


def test_executable_path_parent_traversal_fails_closed(tmp_path: Path) -> None:
    for executable in ("/usr/bin/../pytest", r"C:\Tools\..\pytest.exe"):
        with pytest.raises(ValueError, match="parent traversal"):
            SandboxPolicy(
                workspace_root=tmp_path,
                allowed_executables=(executable,),
            )

    sandbox = SandboxPolicy(
        workspace_root=tmp_path,
        allowed_executables=("/usr/bin/pytest",),
    )
    with pytest.raises(PermissionError, match="process executable"):
        sandbox.authorize_executable("/usr/bin/../pytest")


def test_empty_executable_allowlist_denies_every_process(tmp_path: Path) -> None:
    sandbox = SandboxPolicy(workspace_root=tmp_path)
    with pytest.raises(PermissionError, match="process executable"):
        sandbox.authorize_executable("pytest")


def test_high_impact_approval_is_exact_expiring_and_single_use(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    intent = ActionIntent(
        action_id="danger-1",
        tool_id="danger.execute",
        risk=ToolRisk.HIGH_IMPACT,
        target="named operation",
    )
    ledger = ExecutionBudgetLedger(policy.budget)
    approvals = ApprovalLedger()
    now = datetime(2026, 8, 18, 19, 0, tzinfo=UTC)

    with pytest.raises(PermissionError, match="explicit approval"):
        authorize_action(intent, policy, ledger, approvals, now=now)

    evidence = _approval(intent)
    authorize_action(intent, policy, ledger, approvals, approval=evidence, now=now)
    with pytest.raises(PermissionError, match="already used"):
        authorize_action(intent, policy, ledger, approvals, approval=evidence, now=now)

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
            approval=_approval(intent, approval_id="approval-2"),
            now=now,
        )


def test_expired_approval_is_rejected(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    intent = ActionIntent(
        action_id="danger-expired",
        tool_id="danger.execute",
        risk=ToolRisk.HIGH_IMPACT,
        target="operation",
    )
    approval = _approval(intent)
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
